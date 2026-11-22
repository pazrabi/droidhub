/*
 * macos_hub_xpc_client.c
 *
 * Python-callable C client transport for the DroidHub stable Hub helper.
 * Compiled as a dylib; exposes one public symbol: dh_hub_list_devices().
 *
 * ABI
 * ---
 *   int dh_hub_list_devices(
 *       const char *label,              // Mach service label (see allowlist)
 *       const char *server_requirement, // non-empty code-signing requirement
 *       void       *out,                // caller-allocated receive buffer
 *       size_t      capacity,           // size of *out in bytes
 *       size_t     *written,            // bytes written to *out on success
 *       int        *server_status       // server-level status code
 *   );
 *
 * Transport return codes (*out_code / return value):
 *   0  ok
 *   1  XPC service unavailable / connection failed
 *   2  timed out (~10 s)
 *   3  protocol / malformed reply
 *   4  output buffer too small
 *
 * Server status codes (*server_status, 0 on transport failure):
 *   0  OK   1  needs authorisation   2  temporary error
 *   3  rejected   4  invalid upstream
 *
 * Allowed labels:
 *   Production: site.molokohub.droidhub.hub-helper.v1
 *   Test:       site.molokohub.droidhub.xpc-test.<non-empty suffix>
 *
 * Reply protocol:
 *   Request:  {"operation":"list_devices","version":uint64(1)}
 *   Response: {"version":uint64(1),"status":int64(code),"body":data}
 *     body present and copied only when status == 0, bounded to 2 MiB.
 *     Non-zero status with any body present is a protocol error.
 *
 * Compile (macOS 12+, Xcode CLT):
 *   clang -std=c17 -Wall -Wextra -Wpedantic -Werror               \
 *         -fblocks -mmacosx-version-min=12.0                       \
 *         -dynamiclib -install_name @rpath/macos_hub_xpc_client.dylib \
 *         -o macos_hub_xpc_client.dylib                            \
 *         macos_hub_xpc_client.c
 *
 * LINUX LIMITATION: XPC/Dispatch are Apple-only.  The Linux stub at the
 * bottom returns DH_ERR_UNAVAILABLE.  Native compile/smoke-tests on macOS.
 */

/* =========================================================================
 * macOS implementation
 * ====================================================================== */
#ifdef __APPLE__

#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <dispatch/dispatch.h>
#include <xpc/xpc.h>

/* -------------------------------------------------------------------------
 * Public ABI visibility
 * ---------------------------------------------------------------------- */
#define DH_EXPORT __attribute__((visibility("default")))

/* -------------------------------------------------------------------------
 * Constants
 * ---------------------------------------------------------------------- */
#define LABEL_PRODUCTION  "site.molokohub.droidhub.hub-helper.v1"
#define LABEL_TEST_PREFIX "site.molokohub.droidhub.xpc-test."

/** Maximum body the transport accepts from the server (2 MiB). */
#define BODY_MAX_BYTES    ((size_t)(2u * 1024u * 1024u))

/** Nanoseconds the client waits for a reply before returning code 2. */
#define REPLY_TIMEOUT_NS  ((int64_t)10 * 1000000000LL)

/* -------------------------------------------------------------------------
 * Transport return codes
 * ---------------------------------------------------------------------- */
enum dh_transport_code {
    DH_OK               = 0,
    DH_ERR_UNAVAILABLE  = 1,
    DH_ERR_TIMEOUT      = 2,
    DH_ERR_PROTOCOL     = 3,
    DH_ERR_TOO_SMALL    = 4,
};

#define SERVER_STATUS_MAX 4

/* -------------------------------------------------------------------------
 * Label allowlist guard
 * ---------------------------------------------------------------------- */
static int check_label(const char *label)
{
    if (label == NULL || label[0] == '\0')
        return -1;
    if (strcmp(label, LABEL_PRODUCTION) == 0)
        return 0;
    size_t plen = sizeof(LABEL_TEST_PREFIX) - 1;
    if (strncmp(label, LABEL_TEST_PREFIX, plen) == 0 && label[plen] != '\0')
        return 0;
    return -1;
}

/* -------------------------------------------------------------------------
 * Heap-allocated shared state with atomic dual-ownership
 *
 * Both the caller and the reply callback hold one reference each (refcount
 * starts at 2).  Whichever drops the last reference frees the struct.
 * The reply callback retains the received XPC reply object, signals the
 * semaphore, then drops its reference.  The caller waits on the semaphore,
 * reads results, then drops its reference.
 *
 * This eliminates the dangling-pointer hazard: on timeout the caller
 * returns while the callback may still be in-flight, but the heap block
 * remains alive until the callback eventually fires and drops its ref.
 * The body is NOT embedded in this struct (no 2 MiB stack/heap monolith);
 * instead the callback retains the XPC reply object and the caller copies
 * directly from it after the wait.
 * ---------------------------------------------------------------------- */
typedef struct {
    atomic_int           refcount;  /* starts at 2 */
    dispatch_semaphore_t sem;
    atomic_int           done;      /* 0 → waiting, 1 → signalled */

    /* Results written by callback before signalling, read by caller after */
    int            transport_code;
    int            server_status;
    xpc_object_t   reply;           /* retained by callback; NULL on error */
} dh_state_t;

static void dh_state_release(dh_state_t *s)
{
    if (atomic_fetch_sub(&s->refcount, 1) == 1) {
        /* Last owner: clean up */
        if (s->reply)
            xpc_release(s->reply);
        dispatch_release(s->sem);
        free(s);
    }
}

/* -------------------------------------------------------------------------
 * dh_hub_list_devices
 * ---------------------------------------------------------------------- */
DH_EXPORT int dh_hub_list_devices(
    const char *label,
    const char *server_requirement,
    void       *out,
    size_t      capacity,
    size_t     *written,
    int        *server_status)
{
    /* Zero all outputs unconditionally. */
    if (written)       *written       = 0;
    if (server_status) *server_status = 0;

    /* --- Validate inputs ------------------------------------------------- */
    if (out == NULL || written == NULL || server_status == NULL)
        return DH_ERR_PROTOCOL;

    if (check_label(label) != 0)
        return DH_ERR_UNAVAILABLE;

    if (server_requirement == NULL || server_requirement[0] == '\0')
        return DH_ERR_UNAVAILABLE;

    /* --- Allocate per-call state ----------------------------------------- */
    dh_state_t *s = calloc(1, sizeof(dh_state_t));
    if (s == NULL)
        return DH_ERR_UNAVAILABLE;

    atomic_init(&s->refcount, 2);  /* caller ref + callback ref */
    atomic_init(&s->done, 0);
    s->transport_code = DH_ERR_TIMEOUT;
    s->reply          = NULL;

    s->sem = dispatch_semaphore_create(0);
    if (s->sem == NULL) {
        free(s);
        return DH_ERR_UNAVAILABLE;
    }

    /* --- Create XPC connection ------------------------------------------- */
    xpc_connection_t conn =
        xpc_connection_create_mach_service(label, NULL, 0);
    if (conn == NULL) {
        /* refcount is 2, but no callback will fire; release both refs */
        atomic_store(&s->refcount, 1);
        dh_state_release(s);
        return DH_ERR_UNAVAILABLE;
    }

    /* Apply peer code-signing requirement BEFORE activation.
     * Fail closed: if the requirement cannot be installed, do not proceed. */
    if (xpc_connection_set_peer_code_signing_requirement(
            conn, server_requirement) != 0) {
        xpc_release(conn);
        atomic_store(&s->refcount, 1);
        dh_state_release(s);
        return DH_ERR_UNAVAILABLE;
    }

    /* Set event handler AFTER requirement is successfully installed. */
    xpc_connection_set_event_handler(conn, ^(xpc_object_t event) {
        (void)event;
    });

    xpc_connection_activate(conn);

    /* --- Build request --------------------------------------------------- */
    xpc_object_t request = xpc_dictionary_create(NULL, NULL, 0);
    if (request == NULL) {
        xpc_connection_cancel(conn);
        xpc_release(conn);
        atomic_store(&s->refcount, 1);
        dh_state_release(s);
        return DH_ERR_UNAVAILABLE;
    }
    xpc_dictionary_set_string(request, "operation", "list_devices");
    xpc_dictionary_set_uint64(request, "version",   UINT64_C(1));

    /* --- Send with reply block ------------------------------------------- */
    xpc_connection_send_message_with_reply(
        conn, request, NULL, ^(xpc_object_t reply) {

        int code       = DH_ERR_PROTOCOL;
        int srv_status = 0;
        xpc_object_t retained_reply = NULL;

        if (xpc_get_type(reply) == XPC_TYPE_ERROR) {
            code = DH_ERR_UNAVAILABLE;

        } else if (xpc_get_type(reply) != XPC_TYPE_DICTIONARY) {
            code = DH_ERR_PROTOCOL;

        } else {
            /* Validate version: must be present, uint64, value == 1 */
            xpc_object_t ver_obj = xpc_dictionary_get_value(reply, "version");
            if (ver_obj == NULL || xpc_get_type(ver_obj) != XPC_TYPE_UINT64) {
                code = DH_ERR_PROTOCOL;
                goto signal;
            }
            if (xpc_uint64_get_value(ver_obj) != UINT64_C(1)) {
                code = DH_ERR_PROTOCOL;
                goto signal;
            }

            /* Validate status: must be present, int64, in range */
            xpc_object_t st_obj = xpc_dictionary_get_value(reply, "status");
            if (st_obj == NULL || xpc_get_type(st_obj) != XPC_TYPE_INT64) {
                code = DH_ERR_PROTOCOL;
                goto signal;
            }
            int64_t raw_status = xpc_int64_get_value(st_obj);
            if (raw_status < 0 || raw_status > SERVER_STATUS_MAX) {
                code = DH_ERR_PROTOCOL;
                goto signal;
            }
            srv_status = (int)raw_status;

            if (srv_status == 0) {
                /* Success: body must be present and XPC_TYPE_DATA */
                xpc_object_t body_obj =
                    xpc_dictionary_get_value(reply, "body");
                if (body_obj == NULL ||
                    xpc_get_type(body_obj) != XPC_TYPE_DATA) {
                    code = DH_ERR_PROTOCOL;
                    goto signal;
                }
                if (xpc_data_get_length(body_obj) > BODY_MAX_BYTES) {
                    code = DH_ERR_PROTOCOL;
                    goto signal;
                }
                /* Retain the reply so the caller can copy body after wait */
                retained_reply = xpc_retain(reply);
            } else {
                /* Non-zero status: body must NOT be present */
                if (xpc_dictionary_get_value(reply, "body") != NULL) {
                    code = DH_ERR_PROTOCOL;
                    goto signal;
                }
            }
            code = DH_OK;
        }

signal:;
        int expected = 0;
        if (atomic_compare_exchange_strong(&s->done, &expected, 1)) {
            s->transport_code = code;
            s->server_status  = srv_status;
            s->reply          = retained_reply; /* may be NULL */
            retained_reply    = NULL;
            dispatch_semaphore_signal(s->sem);
        } else if (retained_reply) {
            xpc_release(retained_reply);
        }
        /* Drop callback's reference; may free s if caller timed out */
        dh_state_release(s);
    });

    xpc_release(request);

    /* --- Wait for reply or timeout --------------------------------------- */
    dispatch_time_t deadline =
        dispatch_time(DISPATCH_TIME_NOW, REPLY_TIMEOUT_NS);
    long timed_out = dispatch_semaphore_wait(s->sem, deadline);

    xpc_connection_cancel(conn);
    xpc_release(conn);

    /* --- Decode results -------------------------------------------------- */
    int final_rc    = DH_ERR_TIMEOUT;
    int final_srv   = 0;

    if (timed_out == 0) {
        final_rc  = s->transport_code;
        final_srv = s->server_status;
    }

    /* Copy body before releasing state (which may free s->reply) */
    if (timed_out == 0 && final_rc == DH_OK && final_srv == 0 && s->reply) {
        xpc_object_t body_obj = xpc_dictionary_get_value(s->reply, "body");
        size_t blen = (body_obj != NULL) ? xpc_data_get_length(body_obj) : 0;
        if (blen > capacity) {
            dh_state_release(s);
            return DH_ERR_TOO_SMALL;
        }
        if (body_obj != NULL)
            memcpy(out, xpc_data_get_bytes_ptr(body_obj), blen);
        *written = blen;
    }

    /* Drop caller's reference; may free s if callback already fired */
    dh_state_release(s);

    if (timed_out != 0)
        return DH_ERR_TIMEOUT;

    if (final_rc != DH_OK)
        return final_rc;

    *server_status = final_srv;
    return DH_OK;
}

#else /* !__APPLE__ */

/* =========================================================================
 * Linux / non-macOS stub
 * =========================================================================
 * XPC and Dispatch are Apple-only APIs that ship with macOS libSystem.
 * Linux CI uses this stub; native compile/smoke-tests are on macOS.
 */

#include <stddef.h>
#include <stdio.h>

__attribute__((visibility("default")))
int dh_hub_list_devices(
    const char *label,
    const char *server_requirement,
    void       *out,
    size_t      capacity,
    size_t     *written,
    int        *server_status)
{
    (void)label; (void)server_requirement;
    (void)out;   (void)capacity;
    (void)written; (void)server_status;
    fprintf(stderr,
        "dh_hub_list_devices: requires macOS 12+ "
        "(XPC/Dispatch unavailable on Linux).\n"
        "Evaluate the #ifdef __APPLE__ section for correctness.\n");
    return 1; /* DH_ERR_UNAVAILABLE */
}

#endif /* __APPLE__ */
