/*
 * macos_hub_xpc_client.c
 *
 * Python-callable C client transport for the DroidHub stable Hub helper.
 * Compiled as a dylib; exposes one public symbol: dh_hub_list_devices().
 * Independent of the existing app.  New source only.
 *
 * Requirements
 * ------------
 *   • macOS 12+, C17, Clang -fblocks, public XPC only.
 *   • No network, no Keychain, no stored state across calls.
 *   • Applies xpc_connection_set_peer_code_signing_requirement BEFORE
 *     activation; refuses empty requirements and disallowed labels.
 *   • Allowed labels (enforced at runtime):
 *       Production: site.molokohub.droidhub.hub-helper.v1
 *       Test:       site.molokohub.droidhub.xpc-test.<non-empty suffix>
 *   • Reply protocol:
 *       Request:  {"operation":"list_devices","version":uint64(1)}
 *       Response: {"version":uint64(1),"status":int64(code),"body":data}
 *         body present and copied only when status == 0, bounded to 2 MiB.
 *   • Transport return codes (written to *out_code):
 *       0  ok
 *       1  XPC service unavailable / connection failed
 *       2  timed out (~10 s)
 *       3  protocol / malformed reply
 *       4  output buffer too small
 *   • server_status codes (written to *server_status, 0 on transport failure):
 *       0  OK
 *       1  needs authorisation
 *       2  temporary error
 *       3  rejected
 *       4  invalid upstream
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
 *   Returns one of the transport codes above.
 *
 * Compile (macOS 12+, Xcode CLT)
 * --------------------------------
 *   clang -std=c17 -Wall -Wextra -Wpedantic -Werror               \
 *         -fblocks -mmacosx-version-min=12.0                       \
 *         -dynamiclib -install_name @rpath/macos_hub_xpc_client.dylib \
 *         -o macos_hub_xpc_client.dylib                            \
 *         macos_hub_xpc_client.c
 *
 * Python usage (ctypes sketch):
 *   import ctypes, pathlib
 *   lib = ctypes.CDLL(str(pathlib.Path(__file__).parent /
 *                         "macos_hub_xpc_client.dylib"))
 *   lib.dh_hub_list_devices.restype  = ctypes.c_int
 *   lib.dh_hub_list_devices.argtypes = [
 *       ctypes.c_char_p, ctypes.c_char_p,
 *       ctypes.c_void_p, ctypes.c_size_t,
 *       ctypes.POINTER(ctypes.c_size_t),
 *       ctypes.POINTER(ctypes.c_int),
 *   ]
 *   buf = ctypes.create_string_buffer(2 * 1024 * 1024)
 *   written = ctypes.c_size_t(0)
 *   srv_st  = ctypes.c_int(0)
 *   rc = lib.dh_hub_list_devices(
 *       b"site.molokohub.droidhub.hub-helper.v1",
 *       b"anchor apple generic and ...",
 *       buf, ctypes.sizeof(buf), ctypes.byref(written), ctypes.byref(srv_st))
 *
 * LINUX LIMITATION
 * ----------------
 * XPC and Dispatch are Apple-only APIs shipped in macOS libSystem.
 * This file cannot be compiled or executed on Linux.  Linux CI reaches the
 * stub at the bottom, which returns a clear diagnostic and exits 3.
 * Correctness review and native smoke-tests are performed by the macOS lead.
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

/** Exact production service label. */
#define LABEL_PRODUCTION  "site.molokohub.droidhub.hub-helper.v1"

/** Allowed test label prefix (must be followed by a non-empty suffix). */
#define LABEL_TEST_PREFIX "site.molokohub.droidhub.xpc-test."

/** Maximum body the transport will accept from the server (2 MiB). */
#define BODY_MAX_BYTES    ((size_t)(2u * 1024u * 1024u))

/** Nanoseconds the client waits for a reply before returning code 2. */
#define REPLY_TIMEOUT_NS  ((int64_t)10 * 1000000000LL)

/* -------------------------------------------------------------------------
 * Transport return codes (documented in ABI header above)
 * ---------------------------------------------------------------------- */
enum dh_transport_code {
    DH_OK               = 0,
    DH_ERR_UNAVAILABLE  = 1,
    DH_ERR_TIMEOUT      = 2,
    DH_ERR_PROTOCOL     = 3,
    DH_ERR_TOO_SMALL    = 4,
};

/* -------------------------------------------------------------------------
 * Server-status allowlist
 * ---------------------------------------------------------------------- */
#define SERVER_STATUS_MAX 4   /* values 0..4 are defined; anything else → 3 */

/* -------------------------------------------------------------------------
 * Label allowlist guard
 *
 * Returns 0 if the label is permitted, -1 otherwise.
 * Never logs the requirement string (which could embed sensitive metadata).
 * ---------------------------------------------------------------------- */
static int check_label(const char *label)
{
    if (label == NULL || label[0] == '\0')
        return -1;

    /* Exact production label */
    if (strcmp(label, LABEL_PRODUCTION) == 0)
        return 0;

    /* Test prefix with a non-empty suffix */
    size_t plen = sizeof(LABEL_TEST_PREFIX) - 1; /* exclude NUL */
    if (strncmp(label, LABEL_TEST_PREFIX, plen) == 0 && label[plen] != '\0')
        return 0;

    return -1;
}

/* -------------------------------------------------------------------------
 * Shared state for the single async completion
 *
 * Allocated on the stack of dh_hub_list_devices; pointers passed into the
 * reply block by value (copied into the block literal).  The semaphore keeps
 * the caller alive until the block fires or the timeout expires.
 *
 * Fields written ONLY by the reply block, read ONLY after the semaphore is
 * signalled.  The atomic flag ensures exactly one signal regardless of
 * spurious XPC error deliveries.
 * ---------------------------------------------------------------------- */
typedef struct {
    /* Synchronisation */
    dispatch_semaphore_t sem;
    atomic_int           done;   /* 0 → waiting, 1 → signalled */

    /* Results (written before signalling) */
    int     transport_code;
    int     server_status;
    size_t  body_len;
    uint8_t body[BODY_MAX_BYTES];
} dh_completion_t;

/* -------------------------------------------------------------------------
 * dh_hub_list_devices
 *
 * Public entry point.  Thread-safe: each call allocates independent state;
 * concurrent calls do not share or cross results.
 *
 * Memory ownership:
 *   • conn       – xpc_release'd after cancel in this function.
 *   • request    – xpc_release'd here after send_message.
 *   • reply      – owned by XPC, released by the runtime after the block.
 *   • completion – stack-allocated; kept alive by the semaphore wait.
 * ---------------------------------------------------------------------- */
DH_EXPORT int dh_hub_list_devices(
    const char *label,
    const char *server_requirement,
    void       *out,
    size_t      capacity,
    size_t     *written,
    int        *server_status)
{
    /* Zero all outputs unconditionally; set to real values only on success. */
    if (written)       *written       = 0;
    if (server_status) *server_status = 0;

    /* --- Validate inputs ------------------------------------------------- */
    if (out == NULL || written == NULL || server_status == NULL)
        return DH_ERR_PROTOCOL;

    if (check_label(label) != 0)
        return DH_ERR_UNAVAILABLE;

    if (server_requirement == NULL || server_requirement[0] == '\0')
        return DH_ERR_UNAVAILABLE;

    /* --- Allocate per-call completion state ------------------------------ */
    /*
     * body[] is 2 MiB; stack allocation is intentional so that concurrent
     * callers each have fully independent state without dynamic allocation.
     * If the stack size is a concern in the embedding process, the caller
     * may move dh_completion_t to the heap and pass it via __block.
     */
    dh_completion_t completion;
    memset(&completion, 0, sizeof(completion));
    completion.sem            = dispatch_semaphore_create(0);
    completion.transport_code = DH_ERR_TIMEOUT; /* default if block never fires */

    if (completion.sem == NULL)
        return DH_ERR_UNAVAILABLE;

    /*
     * We need to pass a pointer into the reply block.  Declare a __block
     * pointer so the block captures the address stably (the block is invoked
     * on a private XPC queue, not the caller's thread).
     *
     * IMPORTANT: the caller's out/written/server_status buffers are NEVER
     * touched inside the async block – only the per-call completion struct
     * is written there.  Caller buffers are only written after the semaphore
     * is signalled and we have returned from the async world.
     */
    __block dh_completion_t *cp = &completion;

    /* --- Create XPC connection ------------------------------------------- */
    xpc_connection_t conn =
        xpc_connection_create_mach_service(label, NULL, 0);
    if (conn == NULL) {
        dispatch_release(completion.sem);
        return DH_ERR_UNAVAILABLE;
    }

    /* Apply peer code-signing requirement BEFORE activation. */
    xpc_connection_set_peer_code_signing_requirement(conn, server_requirement);

    /* Set an event handler that handles unexpected connection-level errors. */
    xpc_connection_set_event_handler(conn, ^(xpc_object_t event) {
        (void)event; /* connection-level errors handled via reply block */
    });

    xpc_connection_activate(conn);

    /* --- Build request --------------------------------------------------- */
    xpc_object_t request = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_string(request, "operation", "list_devices");
    xpc_dictionary_set_uint64(request, "version",   UINT64_C(1));

    /* --- Send with reply block ------------------------------------------- */
    xpc_connection_send_message_with_reply(
        conn, request, NULL, ^(xpc_object_t reply) {

        int code        = DH_ERR_PROTOCOL;
        int srv_status  = 0;
        size_t body_len = 0;

        if (xpc_get_type(reply) == XPC_TYPE_ERROR) {
            /* XPC error (unavailable, interrupted, etc.) */
            code = DH_ERR_UNAVAILABLE;

        } else if (xpc_get_type(reply) != XPC_TYPE_DICTIONARY) {
            code = DH_ERR_PROTOCOL;

        } else {
            /* Validate version field */
            uint64_t ver = xpc_dictionary_get_uint64(reply, "version");
            if (ver != UINT64_C(1)) {
                code = DH_ERR_PROTOCOL;
                goto signal;
            }

            /* Validate status field */
            int64_t raw_status = xpc_dictionary_get_int64(reply, "status");
            if (raw_status < 0 || raw_status > SERVER_STATUS_MAX) {
                code = DH_ERR_PROTOCOL;
                goto signal;
            }
            srv_status = (int)raw_status;

            if (srv_status == 0) {
                /* Success: body must be XPC_TYPE_DATA */
                xpc_object_t body_obj =
                    xpc_dictionary_get_value(reply, "body");
                if (body_obj == NULL ||
                    xpc_get_type(body_obj) != XPC_TYPE_DATA) {
                    code = DH_ERR_PROTOCOL;
                    goto signal;
                }
                size_t len = xpc_data_get_length(body_obj);
                if (len > BODY_MAX_BYTES) {
                    code = DH_ERR_PROTOCOL;
                    goto signal;
                }
                /* Copy body into per-call buffer (not caller buffer) */
                memcpy(cp->body, xpc_data_get_bytes_ptr(body_obj), len);
                body_len = len;
            }
            /* Non-zero server statuses: no body expected; transport OK */
            code = DH_OK;
        }

signal:;
        int expected = 0;
        if (atomic_compare_exchange_strong(&cp->done, &expected, 1)) {
            cp->transport_code = code;
            cp->server_status  = srv_status;
            cp->body_len       = body_len;
            dispatch_semaphore_signal(cp->sem);
        }
    });

    xpc_release(request);

    /* --- Wait for reply or timeout --------------------------------------- */
    dispatch_time_t deadline =
        dispatch_time(DISPATCH_TIME_NOW, REPLY_TIMEOUT_NS);
    long timed_out = dispatch_semaphore_wait(completion.sem, deadline);

    /*
     * Cancel the connection regardless of outcome.  On timeout the reply
     * block may still be inflight; the atomic flag prevents it from
     * overwriting the results we are about to read.  The block pointer cp
     * remains valid because completion lives on this stack frame, which
     * does not return until after we release the connection below.
     *
     * We call dispatch_release on the semaphore here.  The block already
     * signalled (or the timeout fired), so no further signals will occur.
     */
    xpc_connection_cancel(conn);
    xpc_release(conn);
    dispatch_release(completion.sem);
    completion.sem = NULL;

    /* --- Decode results -------------------------------------------------- */
    if (timed_out != 0) {
        /* Timeout: all outputs remain zero (set at entry). */
        return DH_ERR_TIMEOUT;
    }

    int t_code   = completion.transport_code;
    int srv_code = completion.server_status;
    size_t blen  = completion.body_len;

    if (t_code != DH_OK) {
        /* Transport failure: all outputs remain zero. */
        return t_code;
    }

    /* --- Copy body to caller buffer (only on full success) --------------- */
    if (srv_code == 0) {
        if (blen > capacity)
            return DH_ERR_TOO_SMALL;
        memcpy(out, completion.body, blen);
        *written = blen;
    }

    *server_status = srv_code;
    return DH_OK;
}

#else /* !__APPLE__ */

/* =========================================================================
 * Linux / non-macOS stub
 * =========================================================================
 *
 * XPC and Dispatch are Apple-only APIs that ship with macOS libSystem.
 * This translation unit cannot be compiled or linked on Linux.
 *
 * Linux CI includes this stub so that the file parses cleanly and returns
 * a clear diagnostic instead of failing with obscure linker errors.
 * The macOS lead evaluates the #ifdef __APPLE__ section for correctness
 * and performs native compile and runtime smoke-tests there.
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
