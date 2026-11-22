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
 *   -framework Security
 *
 * Note: libxpc and libdispatch are part of libSystem, linked by default on macOS.
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

#define MAX_BODY_BYTES (2 * 1024 * 1024) /* 2 MiB */

static const char *const VALID_LABEL_PREFIX_TEST = "site.molokohub.droidhub.xpc-test.";
static const char *const VALID_LABEL_PRODUCTION  = "site.molokohub.droidhub.hub-helper.v1";

typedef struct {
    int (*read_inventory)(void *context, void *out, size_t capacity, size_t *written);
    void *context;
    SecRequirementRef requirement;
    dispatch_queue_t worker_queue;
    atomic_flag in_flight;
} hub_listener_state_t;

typedef struct {
    hub_listener_state_t *state;
    xpc_connection_t connection;
} peer_context_t;

typedef struct {
    hub_listener_state_t *state;
    xpc_connection_t connection;
    xpc_object_t request;
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

static void send_reply(xpc_connection_t connection, xpc_object_t request, int64_t status, const void *body, size_t body_len) {
    xpc_object_t reply = xpc_dictionary_create_reply(request);
    if (!reply) {
        return;
    }

    xpc_dictionary_set_uint64(reply, "version", 1);
    xpc_dictionary_set_int64(reply, "status", status);

    if (status == 0 && body != NULL && body_len > 0) {
        xpc_dictionary_set_data(reply, "body", body, body_len);
    }

    xpc_connection_send_message(connection, reply);
    xpc_release(reply);
}

static void handle_request_worker(void *context) {
    work_item_t *item = (work_item_t *)context;
    hub_listener_state_t *state = item->state;
    xpc_connection_t connection = item->connection;
    xpc_object_t request = item->request;

    void *buffer = malloc(MAX_BODY_BYTES);
    if (!buffer) {
        atomic_flag_clear(&state->in_flight);
        send_reply(connection, request, 2, NULL, 0);
        xpc_release(connection);
        xpc_release(request);
        free(item);
        return;
    }

    size_t written = 0;
    int rc = state->read_inventory(state->context, buffer, MAX_BODY_BYTES, &written);

    atomic_flag_clear(&state->in_flight);

    if (rc == 0) {
        if (written > 0 && written <= MAX_BODY_BYTES) {
            send_reply(connection, request, 0, buffer, written);
        } else {
            /* Require nonempty success, bound body 2 MiB */
            send_reply(connection, request, 4, NULL, 0);
        }
    } else if (rc >= 1 && rc <= 4) {
        send_reply(connection, request, (int64_t)rc, NULL, 0);
    } else {
        /* Any unexpected provider error mapped to 4 (invalid response/protocol) */
        send_reply(connection, request, 4, NULL, 0);
    }

    free(buffer);
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
     * Concurrency limit: At most one provider call in flight across clients.
     * Return status 2 immediately if already running.
     */
    if (atomic_flag_test_and_set(&state->in_flight)) {
        send_reply(connection, event, 2, NULL, 0);
        return;
    }

    /* Retain connection and request before dispatching off the listener queue */
    xpc_retain(connection);
    xpc_retain(event);

    work_item_t *item = (work_item_t *)malloc(sizeof(work_item_t));
    if (!item) {
        atomic_flag_clear(&state->in_flight);
        xpc_release(connection);
        xpc_release(event);
        send_reply(connection, event, 2, NULL, 0);
        return;
    }

    item->state = state;
    item->connection = connection;
    item->request = event;

    dispatch_async_f(state->worker_queue, item, handle_request_worker);
}

int dh_hub_serve(
    const char *label,
    const char *client_requirement,
    int (*read_inventory)(void *context, void *out, size_t capacity, size_t *written),
    void *context
) {
    if (!is_valid_label(label)) {
        return -1;
    }
    if (!client_requirement || !read_inventory) {
        return -1;
    }

    CFStringRef req_str = CFStringCreateWithCString(kCFAllocatorDefault, client_requirement, kCFStringEncodingUTF8);
    if (!req_str) {
        return -1;
    }

    SecRequirementRef requirement = NULL;
    OSStatus status = SecRequirementCreateWithString(req_str, kSecCSDefaultFlags, &requirement);
    CFRelease(req_str);
    if (status != errSecSuccess || !requirement) {
        return -1;
    }

    hub_listener_state_t *state = (hub_listener_state_t *)malloc(sizeof(hub_listener_state_t));
    if (!state) {
        CFRelease(requirement);
        return -1;
    }

    state->read_inventory = read_inventory;
    state->context = context;
    state->requirement = requirement;
    atomic_flag_clear(&state->in_flight);

    state->worker_queue = dispatch_queue_create("site.molokohub.droidhub.worker", DISPATCH_QUEUE_SERIAL);
    if (!state->worker_queue) {
        CFRelease(requirement);
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
        CFRelease(requirement);
        free(state);
        return -1;
    }

    xpc_connection_set_event_handler(listener, ^(xpc_object_t peer) {
        if (xpc_get_type(peer) != XPC_TYPE_CONNECTION) {
            return;
        }

        xpc_connection_t peer_conn = (xpc_connection_t)peer;

        /* Enforce code signing requirement before activation */
        int cs_err = xpc_connection_set_peer_code_signing_requirement(peer_conn, state->requirement);
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
