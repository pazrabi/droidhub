/*
 * macos_hub_peer_probe.c
 *
 * TEST-ONLY diagnostic for macOS XPC peer code-signing requirement smoke tests.
 * NOT production code.  Exercises xpc_connection_set_peer_code_signing_requirement(3)
 * (macOS 12+) in isolation – no Keychain, no network, no production service.
 *
 * Usage
 * -----
 *   serve <mach-service-label> <client-code-requirement-string>
 *   ping  <mach-service-label> <server-code-requirement-string>
 *
 * <mach-service-label> MUST begin with the prefix enforced below.
 * <*-code-requirement-string> is a standard Apple Code Signing Requirement
 * language string, e.g. "anchor apple generic".
 *
 * Exit codes
 * ----------
 *   0  – success (serve: listener running / ping: pong received)
 *   1  – usage or argument error
 *   2  – label prefix guard triggered
 *   3  – XPC setup error
 *   4  – ping timeout (no server reply within PING_TIMEOUT_SEC)
 *   5  – ping XPC error (peer rejected or connection failed)
 *   6  – ping unexpected / invalid reply
 *
 * Compile (macOS 12+, Xcode CLT)
 * --------------------------------
 *   clang -std=c17 -Wall -Wextra -Wpedantic -Werror \
 *         -fblocks -mmacosx-version-min=12.0 \
 *         -o macos_hub_peer_probe \
 *         macos_hub_peer_probe.c
 *
 * The lead registers a temporary per-user launchd MachServices job, then runs
 * two independently signed different-cdhash clients and one ad-hoc client to
 * verify mutual code requirements in both directions.
 *
 * LINUX LIMITATION
 * ----------------
 * This file cannot be compiled or executed on Linux.  The XPC and Mach APIs
 * (xpc_connection_create_mach_service, xpc_connection_set_peer_code_signing_requirement,
 * dispatch_main, etc.) are part of Apple's libxpc/libSystem and ship only with
 * macOS.  Linux CI reaches the stub below and exits with a diagnostic.
 * Evaluate the macOS source above for correctness; the macOS lead performs
 * native compile and runtime tests.
 */

#ifdef __APPLE__

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>

#include <dispatch/dispatch.h>
#include <xpc/xpc.h>

/* -------------------------------------------------------------------------
 * Constants
 * ---------------------------------------------------------------------- */

/** All test Mach service labels must begin with this prefix. */
#define LABEL_PREFIX "site.molokohub.droidhub.xpc-test."

/** Ping message key / value */
#define MSG_KEY   "msg"
#define MSG_PING  "ping"
#define MSG_PONG  "pong"

/** Seconds the client waits for a pong before timing out (exit 4). */
#define PING_TIMEOUT_SEC 5

/* -------------------------------------------------------------------------
 * Exit codes
 * ---------------------------------------------------------------------- */
enum {
    EXIT_OK           = 0,
    EXIT_USAGE        = 1,
    EXIT_LABEL_GUARD  = 2,
    EXIT_XPC_SETUP    = 3,
    EXIT_TIMEOUT      = 4,
    EXIT_XPC_ERROR    = 5,
    EXIT_BAD_MSG      = 6,
};

/* -------------------------------------------------------------------------
 * Helpers
 * ---------------------------------------------------------------------- */

static void enforce_label_prefix(const char *label)
{
    if (strncmp(label, LABEL_PREFIX, strlen(LABEL_PREFIX)) != 0) {
        fprintf(stderr,
            "ERROR: label \"%s\" does not begin with required prefix \"%s\".\n"
            "This tool is restricted to test-only service labels.\n",
            label, LABEL_PREFIX);
        exit(EXIT_LABEL_GUARD);
    }
}

/* -------------------------------------------------------------------------
 * serve mode
 *
 * Creates a Mach service listener, applies a peer code-signing requirement
 * on every accepted connection via the public
 * xpc_connection_set_peer_code_signing_requirement(3) API, then activates.
 * Replies to "ping" messages with "pong"; ignores everything else.
 * ---------------------------------------------------------------------- */

static void run_serve(const char *label, const char *peer_requirement)
{
    enforce_label_prefix(label);

    xpc_connection_t listener =
        xpc_connection_create_mach_service(label, NULL,
                                           XPC_CONNECTION_MACH_SERVICE_LISTENER);
    if (listener == NULL) {
        fprintf(stderr, "serve: xpc_connection_create_mach_service failed.\n");
        exit(EXIT_XPC_SETUP);
    }

    xpc_connection_set_event_handler(listener, ^(xpc_object_t event) {
        if (xpc_get_type(event) != XPC_TYPE_CONNECTION) {
            if (xpc_get_type(event) == XPC_TYPE_ERROR) {
                fprintf(stderr, "serve: listener error: %s\n",
                        xpc_dictionary_get_string(event, XPC_ERROR_KEY_DESCRIPTION));
            }
            return;
        }

        xpc_connection_t peer = (xpc_connection_t)event;

        /*
         * Apply the peer code-signing requirement BEFORE activating.
         * If the client does not satisfy this, XPC delivers an error to both
         * sides without any manual verification required.
         */
        if (xpc_connection_set_peer_code_signing_requirement(
                peer, peer_requirement) != 0) {
            fprintf(stderr,
                    "serve: xpc_connection_set_peer_code_signing_requirement "
                    "failed (bad requirement string?): \"%s\"\n",
                    peer_requirement);
            xpc_connection_cancel(peer);
            return;
        }

        xpc_connection_set_event_handler(peer, ^(xpc_object_t msg) {
            if (xpc_get_type(msg) == XPC_TYPE_ERROR) {
                /* Peer disconnected or was rejected – nothing to do. */
                return;
            }
            if (xpc_get_type(msg) != XPC_TYPE_DICTIONARY) {
                return;
            }

            const char *val = xpc_dictionary_get_string(msg, MSG_KEY);
            if (val == NULL || strcmp(val, MSG_PING) != 0) {
                return;
            }

            xpc_object_t reply = xpc_dictionary_create_reply(msg);
            if (reply != NULL) {
                xpc_dictionary_set_string(reply, MSG_KEY, MSG_PONG);
                xpc_connection_send_message(peer, reply);
                xpc_release(reply);
            }
        });

        xpc_connection_activate(peer);
    });

    xpc_connection_activate(listener);

    fprintf(stdout,
            "serve: listening on \"%s\"\n  peer requirement: %s\n"
            "Press Ctrl-C to stop.\n",
            label, peer_requirement);

    dispatch_main(); /* Never returns. */
}

/* -------------------------------------------------------------------------
 * ping mode
 *
 * Creates a client connection, applies a peer code-signing requirement on
 * the server, activates, sends one ping, and waits up to PING_TIMEOUT_SEC
 * seconds for a pong.
 *
 * Result ordering and synchronization
 * ------------------------------------
 * The reply block is the primary completion source.  The event handler only
 * logs unexpected XPC errors and signals the semaphore as a fallback.  A
 * flag guards against signalling the semaphore more than once.
 *
 * result and signalled are heap-allocated atomics shared with the blocks.
 * This is required because Clang Blocks capture local variables by const-copy;
 * taking the address of a captured stack _Atomic variable is undefined
 * behaviour and rejected by -Werror.  Heap allocation gives both the calling
 * scope and the blocks a stable, mutable, non-const pointer.
 * ---------------------------------------------------------------------- */

static void run_ping(const char *label, const char *peer_requirement)
{
    enforce_label_prefix(label);

    xpc_connection_t conn =
        xpc_connection_create_mach_service(label, NULL, 0);
    if (conn == NULL) {
        fprintf(stderr, "ping: xpc_connection_create_mach_service failed.\n");
        exit(EXIT_XPC_SETUP);
    }

    if (xpc_connection_set_peer_code_signing_requirement(
            conn, peer_requirement) != 0) {
        fprintf(stderr,
                "ping: xpc_connection_set_peer_code_signing_requirement "
                "failed (bad requirement string?): \"%s\"\n",
                peer_requirement);
        xpc_release(conn);
        exit(EXIT_XPC_SETUP);
    }

    dispatch_semaphore_t sem = dispatch_semaphore_create(0);

    /*
     * Heap-allocate the shared atomics so both the calling scope and the
     * blocks hold a plain pointer (not a const-captured stack address).
     */
    _Atomic int *result    = malloc(sizeof(_Atomic int));
    _Atomic int *signalled = malloc(sizeof(_Atomic int));
    if (result == NULL || signalled == NULL) {
        fprintf(stderr, "ping: malloc failed.\n");
        xpc_release(conn);
        exit(EXIT_XPC_SETUP);
    }
    atomic_store(result,    EXIT_TIMEOUT); /* pessimistic default */
    atomic_store(signalled, 0);

    /*
     * Event handler: diagnostic only for unexpected XPC errors (e.g. peer
     * code-signing requirement failure before the reply arrives).  It signals
     * the semaphore exactly once via the signalled flag.
     */
    xpc_connection_set_event_handler(conn, ^(xpc_object_t event) {
        if (xpc_get_type(event) != XPC_TYPE_ERROR) {
            return;
        }
        fprintf(stderr, "ping: connection error: %s\n",
                xpc_dictionary_get_string(event, XPC_ERROR_KEY_DESCRIPTION));
        int expected = 0;
        if (atomic_compare_exchange_strong(signalled, &expected, 1)) {
            atomic_store(result, EXIT_XPC_ERROR);
            dispatch_semaphore_signal(sem);
        }
    });

    xpc_connection_activate(conn);

    xpc_object_t ping_msg = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_string(ping_msg, MSG_KEY, MSG_PING);

    /*
     * Reply block: primary completion source.  Signals the semaphore exactly
     * once (guarded by signalled) and stores the definitive result.
     */
    xpc_connection_send_message_with_reply(
        conn, ping_msg, NULL, ^(xpc_object_t reply) {
            int code;
            if (xpc_get_type(reply) == XPC_TYPE_ERROR) {
                fprintf(stderr, "ping: reply error: %s\n",
                        xpc_dictionary_get_string(reply,
                                                  XPC_ERROR_KEY_DESCRIPTION));
                code = EXIT_XPC_ERROR;
            } else if (xpc_get_type(reply) != XPC_TYPE_DICTIONARY) {
                fprintf(stderr, "ping: unexpected reply type.\n");
                code = EXIT_BAD_MSG;
            } else {
                const char *val = xpc_dictionary_get_string(reply, MSG_KEY);
                if (val != NULL && strcmp(val, MSG_PONG) == 0) {
                    fprintf(stdout, "ping: pong received.\n");
                    code = EXIT_OK;
                } else {
                    fprintf(stderr, "ping: unexpected reply value \"%s\".\n",
                            val ? val : "(null)");
                    code = EXIT_BAD_MSG;
                }
            }
            int expected = 0;
            if (atomic_compare_exchange_strong(signalled, &expected, 1)) {
                atomic_store(result, code);
                dispatch_semaphore_signal(sem);
            }
        });

    xpc_release(ping_msg);

    dispatch_time_t deadline =
        dispatch_time(DISPATCH_TIME_NOW,
                      (int64_t)PING_TIMEOUT_SEC * NSEC_PER_SEC);
    long timed_out = dispatch_semaphore_wait(sem, deadline);

    xpc_connection_cancel(conn);
    xpc_release(conn);

    int code;
    if (timed_out != 0) {
        fprintf(stderr, "ping: timed out after %d second(s).\n",
                PING_TIMEOUT_SEC);
        code = EXIT_TIMEOUT;
    } else {
        code = atomic_load(result);
    }

    free(result);
    free(signalled);

    exit(code);
}

/* -------------------------------------------------------------------------
 * Entry point
 * ---------------------------------------------------------------------- */

int main(int argc, char *argv[])
{
    if (argc != 4) {
        fprintf(stderr,
            "Usage:\n"
            "  %s serve <mach-service-label> <client-code-requirement>\n"
            "  %s ping  <mach-service-label> <server-code-requirement>\n"
            "\n"
            "Label must begin with: " LABEL_PREFIX "\n"
            "\n"
            "Exit codes: 0 ok  1 usage  2 label-guard  3 xpc-setup"
            "  4 timeout  5 xpc-error  6 bad-reply\n",
            argv[0], argv[0]);
        return EXIT_USAGE;
    }

    const char *mode        = argv[1];
    const char *label       = argv[2];
    const char *requirement = argv[3];

    if (strcmp(mode, "serve") == 0) {
        run_serve(label, requirement);
        /* run_serve calls dispatch_main() and never returns. */
    } else if (strcmp(mode, "ping") == 0) {
        run_ping(label, requirement);
        /* run_ping calls exit() internally. */
    } else {
        fprintf(stderr, "ERROR: unknown mode \"%s\". Use \"serve\" or \"ping\".\n",
                mode);
        return EXIT_USAGE;
    }

    return EXIT_USAGE; /* unreachable; satisfies the compiler */
}

#else /* !__APPLE__ */

/*
 * Linux / non-macOS stub
 * ----------------------
 * This file depends on Apple XPC and Dispatch APIs that exist only in macOS's
 * libSystem.  It cannot be compiled or executed on Linux.
 *
 * Linux CI reaches this stub and exits with a clear diagnostic rather than
 * failing with obscure linker errors.  Evaluate the macOS implementation
 * above for correctness; the macOS lead compiles and runs native smoke tests.
 */

#include <stdio.h>
#include <stdlib.h>

int main(void)
{
    fprintf(stderr,
        "macos_hub_peer_probe: requires macOS 12+ (XPC/Mach APIs unavailable on Linux).\n"
        "Evaluate the #ifdef __APPLE__ section for correctness.\n");
    return 3; /* EXIT_XPC_SETUP */
}

#endif /* __APPLE__ */
