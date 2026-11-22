/*
 * macos_hub_helper_listener.c
 * Native macOS 12+ XPC listener transport for read-only list_devices.
 *
 * ABI:
 *   int dh_hub_serve(
 *       const char *label,
 *       const char *client_requirement,
 *       int (*read_inventory)(void *context, void *out, size_t capacity, size_t *written),
 *       void *context
 *   );
 *
 * Linker flags (macOS):
 *   -framework Security -framework CoreFoundation
 *
 * Note: libxpc and libdispatch are part of libSystem, linked by default on macOS.
 *
 * Security framework usage:
 *   SecRequirementCreateWithString  - validates the requirement expression string
 *     kSecCSDefaultFlags (0)        - no special options; performs standard parse + validation
 *   The resulting SecRequirementRef is used only for validation and immediately released;
 *   the raw requirement string is owned by hub_listener_state_t for XPC use.
 *
 * CoreFoundation usage:
 *   CFStringCreateWithCString       - wraps the requirement C string for Security API ingestion
 *     kCFStringEncodingUTF8         - requirement strings are always UTF-8 on this path
 *   CFRelease                       - balances the Create rule (caller owns the returned ref)
 */

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <stdbool.h>

#if defined(__APPLE__)

#include <dispatch/dispatch.h>
#include <xpc/xpc.h>
#include <xpc/connection.h>
#include <Security/Security.h>
#include <stdatomic.h>
#include <stdlib.h>

#define MAX_BODY_BYTES          (2 * 1024 * 1024) /* 2 MiB */
#define MAX_REQUIREMENT_BYTES   4096              /* max UTF-8 bytes for requirement string */

static const char *const VALID_LABEL_PREFIX_TEST = "site.molokohub.droidhub.xpc-test.";
static const char *const VALID_LABEL_PRODUCTION  = "site.molokohub.droidhub.hub-helper.v1";

typedef struct {
    int (*read_inventory)(void *context, void *out, size_t capacity, size_t *written);
    void *context;
    char *req_text;         /* owned copy of the requirement string; passed to XPC */
    dispatch_queue_t worker_queue;
    atomic_bool in_flight;  /* true while a provider call is executing */
} hub_listener_state_t;

typedef struct {
    hub_listener_state_t *state;
    xpc_connection_t connection;
} peer_context_t;

typedef struct {
    hub_listener_state_t *state;
    xpc_connection_t connection;
    xpc_object_t request;
    xpc_object_t reply;     /* pre-created reply, retained until response is sent */
} work_item_t;

static bool is_valid_label(const char *label) {
    if (!label) {
        return false;
    }
    if (strcmp(label, VALID_LABEL_PRODUCTION) == 0) {
        return true;
    }
    size_t prefix_len = strlen(VALID_LABEL_PREFIX_TEST);
    if (strncmp(label, VALID_LABEL_PREFIX_TEST, prefix_len) == 0) {
        if (label[prefix_len] != '\0') {
            return true;
        }
    }
    return false;
}

/*
 * send_reply_with_object: transmit a pre-created reply dictionary.
 * The caller retains ownership of `reply`; this function does not release it.
 */
static void send_reply_with_object(xpc_connection_t connection, xpc_object_t reply, int64_t status, const void *body, size_t body_len) {
    xpc_dictionary_set_uint64(reply, "version", 1);
    xpc_dictionary_set_int64(reply, "status", status);

    if (status == 0 && body != NULL && body_len > 0) {
        xpc_dictionary_set_data(reply, "body", body, body_len);
    }

    xpc_connection_send_message(connection, reply);
}

/*
 * send_reply: convenience wrapper that creates the reply on the spot.
 * Used for early-rejection paths where no pre-created reply exists.
 * Returns false if the request is one-way (no reply expected).
 */
static bool send_reply(xpc_connection_t connection, xpc_object_t request, int64_t status, const void *body, size_t body_len) {
    xpc_object_t reply = xpc_dictionary_create_reply(request);
    if (!reply) {
        /* One-way message: no reply expected, nothing to send */
        return false;
    }

    send_reply_with_object(connection, reply, status, body, body_len);
    xpc_release(reply);
    return true;
}

static void handle_request_worker(void *context) {
    work_item_t *item = (work_item_t *)context;
    hub_listener_state_t *state = item->state;
    xpc_connection_t connection = item->connection;
    xpc_object_t request = item->request;
    xpc_object_t reply = item->reply;   /* retained; we must release after send */

    void *buffer = malloc(MAX_BODY_BYTES);
    if (!buffer) {
        atomic_store(&state->in_flight, false);
        send_reply_with_object(connection, reply, 2, NULL, 0);
        xpc_release(reply);
        xpc_release(connection);
        xpc_release(request);
        free(item);
        return;
    }

    size_t written = 0;
    int rc = state->read_inventory(state->context, buffer, MAX_BODY_BYTES, &written);

    atomic_store(&state->in_flight, false);

    if (rc == 0) {
        if (written > 0 && written <= MAX_BODY_BYTES) {
            send_reply_with_object(connection, reply, 0, buffer, written);
        } else {
            /* Require nonempty success, bound body 2 MiB */
            send_reply_with_object(connection, reply, 4, NULL, 0);
        }
    } else if (rc >= 1 && rc <= 4) {
        send_reply_with_object(connection, reply, (int64_t)rc, NULL, 0);
    } else {
        /* Any unexpected provider error mapped to 4 (invalid response/protocol) */
        send_reply_with_object(connection, reply, 4, NULL, 0);
    }

    free(buffer);
    xpc_release(reply);
    xpc_release(connection);
    xpc_release(request);
    free(item);
}

static void peer_event_handler(xpc_object_t event, void *context) {
    peer_context_t *pctx = (peer_context_t *)context;
    hub_listener_state_t *state = pctx->state;
    xpc_connection_t connection = pctx->connection;

    xpc_type_t type = xpc_get_type(event);
    if (type == XPC_TYPE_ERROR) {
        return;
    }

    if (type != XPC_TYPE_DICTIONARY) {
        return;
    }

    /*
     * Validate request before provider:
     * - operation: string, must be "list_devices"
     * - version: uint64, must be 1
     */
    const char *op = xpc_dictionary_get_string(event, "operation");
    if (!op || strcmp(op, "list_devices") != 0) {
        send_reply(connection, event, 4, NULL, 0);
        return;
    }

    xpc_object_t ver_obj = xpc_dictionary_get_value(event, "version");
    if (!ver_obj || xpc_get_type(ver_obj) != XPC_TYPE_UINT64) {
        send_reply(connection, event, 4, NULL, 0);
        return;
    }

    uint64_t version = xpc_uint64_get_value(ver_obj);
    if (version != 1) {
        send_reply(connection, event, 4, NULL, 0);
        return;
    }

    /*
     * Pre-create the reply object before any scheduling.
     * xpc_dictionary_create_reply returns NULL for one-way (fire-and-forget)
     * requests which carry no reply context; reject those explicitly.
     */
    xpc_object_t reply = xpc_dictionary_create_reply(event);
    if (!reply) {
        /* One-way request: no reply channel; drop silently */
        return;
    }

    /*
     * Concurrency limit: at most one provider call in flight across clients.
     * Return status 2 immediately if already running.
     */
    bool expected = false;
    if (!atomic_compare_exchange_strong(&state->in_flight, &expected, true)) {
        send_reply_with_object(connection, reply, 2, NULL, 0);
        xpc_release(reply);
        return;
    }

    /* Retain connection and request before dispatching off the listener queue */
    xpc_retain(connection);
    xpc_retain(event);
    /* reply is already retained by xpc_dictionary_create_reply; keep that ref for the worker */

    work_item_t *item = (work_item_t *)malloc(sizeof(work_item_t));
    if (!item) {
        atomic_store(&state->in_flight, false);
        xpc_release(connection);
        xpc_release(event);
        send_reply_with_object(connection, reply, 2, NULL, 0);
        xpc_release(reply);
        return;
    }

    item->state = state;
    item->connection = connection;
    item->request = event;
    item->reply = reply;

    dispatch_async_f(state->worker_queue, item, handle_request_worker);
}

int dh_hub_serve(
    const char *label,
    const char *client_requirement,
    int (*read_inventory)(void *context, void *out, size_t capacity, size_t *written),
    void *context
) {
    /* Label validation is a native (non-API) check; run it first */
    if (!is_valid_label(label)) {
        return -1;
    }
    if (!client_requirement || !read_inventory) {
        return -1;
    }

    /*
     * Bound the requirement string to MAX_REQUIREMENT_BYTES bytes of UTF-8.
     * strnlen caps the scan; if the string reaches the limit without a NUL
     * it is too long. The +1 on malloc leaves room for the NUL terminator.
     */
    size_t req_len = strnlen(client_requirement, MAX_REQUIREMENT_BYTES + 1);
    if (req_len > MAX_REQUIREMENT_BYTES) {
        return -1;
    }

    /*
     * Validate the requirement expression via Security.framework before use.
     *
     * CFStringCreateWithCString / kCFStringEncodingUTF8:
     *   Wraps the C string as a CFStringRef for the Security API.  The Create
     *   rule means we own the returned object and must CFRelease it.
     *
     * SecRequirementCreateWithString / kSecCSDefaultFlags:
     *   Parses and validates the requirement expression.  kSecCSDefaultFlags (0)
     *   selects the standard (non-lenient) parser.  On success we own `req` and
     *   must CFRelease it; we do so immediately after validation because XPC
     *   takes the raw string, not a SecRequirementRef.
     */
    CFStringRef req_cfstr = CFStringCreateWithCString(kCFAllocatorDefault, client_requirement, kCFStringEncodingUTF8);
    if (!req_cfstr) {
        return -1;
    }

    SecRequirementRef req = NULL;
    OSStatus status = SecRequirementCreateWithString(req_cfstr, kSecCSDefaultFlags, &req);
    CFRelease(req_cfstr);
    if (status != errSecSuccess || !req) {
        return -1;
    }
    /* SecRequirementRef no longer needed; XPC takes the string directly */
    CFRelease(req);

    /*
     * Own a copy of the requirement text for the lifetime of the listener.
     * xpc_connection_set_peer_code_signing_requirement takes const char*, so
     * we must keep the string alive as long as any peer connection may be
     * established.
     */
    char *req_text = (char *)malloc(req_len + 1);
    if (!req_text) {
        return -1;
    }
    memcpy(req_text, client_requirement, req_len);
    req_text[req_len] = '\0';

    hub_listener_state_t *state = (hub_listener_state_t *)malloc(sizeof(hub_listener_state_t));
    if (!state) {
        free(req_text);
        return -1;
    }

    state->read_inventory = read_inventory;
    state->context = context;
    state->req_text = req_text;
    /*
     * atomic_bool must be initialized before first use.  Use ATOMIC_VAR_INIT
     * for static-storage compatibility; the assignment form is equally valid
     * for local/heap objects in C11.
     */
    atomic_init(&state->in_flight, false);

    state->worker_queue = dispatch_queue_create("site.molokohub.droidhub.worker", DISPATCH_QUEUE_SERIAL);
    if (!state->worker_queue) {
        free(req_text);
        free(state);
        return -1;
    }

    xpc_connection_t listener = xpc_connection_create_mach_service(
        label,
        NULL,
        XPC_CONNECTION_MACH_SERVICE_LISTENER
    );
    if (!listener) {
        dispatch_release(state->worker_queue);
        free(req_text);
        free(state);
        return -1;
    }

    xpc_connection_set_event_handler(listener, ^(xpc_object_t peer) {
        if (xpc_get_type(peer) != XPC_TYPE_CONNECTION) {
            return;
        }

        xpc_connection_t peer_conn = (xpc_connection_t)peer;

        /*
         * xpc_connection_set_peer_code_signing_requirement(conn, const char *)
         * Associates a code-signing requirement string with the connection;
         * XPC enforces it before delivering any messages from this peer.
         * We pass our owned copy, which outlives all peer connections.
         */
        int cs_err = xpc_connection_set_peer_code_signing_requirement(peer_conn, state->req_text);
        if (cs_err != 0) {
            xpc_connection_cancel(peer_conn);
            return;
        }

        peer_context_t *pctx = (peer_context_t *)malloc(sizeof(peer_context_t));
        if (!pctx) {
            xpc_connection_cancel(peer_conn);
            return;
        }
        pctx->state = state;
        pctx->connection = peer_conn;

        xpc_connection_set_context(peer_conn, pctx);
        xpc_connection_set_finalizer_f(peer_conn, free);

        xpc_connection_set_event_handler(peer_conn, ^(xpc_object_t event) {
            peer_event_handler(event, pctx);
        });

        xpc_connection_activate(peer_conn);
    });

    xpc_connection_activate(listener);

    dispatch_main();
    return 0;
}

#else /* !defined(__APPLE__) */

#include <errno.h>

int dh_hub_serve(
    const char *label,
    const char *client_requirement,
    int (*read_inventory)(void *context, void *out, size_t capacity, size_t *written),
    void *context
) {
    (void)label;
    (void)client_requirement;
    (void)read_inventory;
    (void)context;
    return ENOTSUP;
}

#endif /* __APPLE__ */
