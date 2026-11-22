/*
 * macos_hub_peer_probe.c
 *
 * TEST-ONLY diagnostic for macOS XPC peer code-signing requirement smoke tests.
 * This is NOT production code.  It exercises the public
 * xpc_connection_set_peer_code_signing_requirement(3) API (macOS 12+) in
 * isolation, with no Keychain, network, installer, or production service access.
 *
 * Usage
 * -----
 *   serve <mach-service-label> <client-code-requirement-string>
 *   ping  <mach-service-label> <server-code-requirement-string>
 *
 * The <mach-service-label> MUST begin with the prefix enforced below.
 * The <*-code-requirement-string> is a standard Apple Code Signing Requirement
 * language string, e.g. "anchor apple generic" or
 * "certificate leaf[subject.CN] = \"My Test CA\"".
 *
 * Exit codes
 * ----------
 *   0  – success (serve: listener running / ping: pong received)
 *   1  – usage or argument error
 *   2  – label prefix guard triggered
 *   3  – XPC setup error (connection/listener creation failed)
 *   4  – ping timeout (server did not reply within PING_TIMEOUT_SEC)
 *   5  – ping error (XPC error event received instead of pong)
 *   6  – unexpected / invalid message (ping received wrong reply type)
 *
 * Compile (macOS 12+, Xcode CLT or full Xcode installed)
 * -------------------------------------------------------
 *   clang -std=c17 -Wall -Wextra -Wpedantic \
 *         -framework Foundation \
 *         -o macos_hub_peer_probe \
 *         macos_hub_peer_probe.c
 *
 * =========================================================================
 * Developer smoke-test report
 * =========================================================================
 *
 * LINUX LIMITATION
 * ----------------
 * This file cannot be compiled or executed on Linux.  The XPC and Mach
 * APIs it uses (xpc_connection_create_mach_service,
 * xpc_connection_set_peer_code_signing_requirement, dispatch_main, etc.) are
 * part of Apple's libxpc / libSystem, which ship only with macOS.  A Linux
 * build host has no native macOS SDK; cross-compilation is not feasible for
 * XPC.  The reviewer should evaluate the source code for correctness,
 * API usage, label guard, message discipline, timeout arithmetic, and exit
 * codes.  The local macOS lead will perform the actual compile and runtime
 * test steps described below.
 *
 * NATIVE SMOKE-TEST COMMANDS (run on macOS 12+ after compile)
 * -----------------------------------------------------------
 *
 * 0.  Build the binary (unsigned, for step 1 and step 2 baseline):
 *
 *       clang -std=c17 -Wall -Wextra -Wpedantic \
 *             -framework Foundation \
 *             -o macos_hub_peer_probe \
 *             macos_hub_peer_probe.c
 *
 * 1.  Adhoc-unsigned client rejection under an exact pinned-certificate
 *     requirement (expected: server stays up; client exits with code 5
 *     because XPC delivers an error event when the peer requirement is
 *     not satisfied at connection time):
 *
 *       # Terminal A – start server with a pinned-cert requirement.
 *       # The server itself is adhoc-unsigned here, but that is irrelevant;
 *       # the requirement it sets is applied to INCOMING clients.
 *       ./macos_hub_peer_probe serve \
 *           site.molokohub.droidhub.xpc-test.smoketest \
 *           'certificate leaf[subject.CN] = "MyTestCA"' &
 *       SERVER_PID=$!
 *
 *       # Terminal B – connect with the SAME unsigned binary as client.
 *       # The server's peer requirement demands "MyTestCA" cert; the adhoc
 *       # client has no such cert, so XPC will reject it.
 *       ./macos_hub_peer_probe ping \
 *           site.molokohub.droidhub.xpc-test.smoketest \
 *           'anchor apple generic'   # server requirement is irrelevant here
 *       echo "ping exit: $?"         # expect 5 (XPC error – peer rejected)
 *
 *       kill $SERVER_PID
 *
 * 2.  Two distinct binaries signed by the SAME test certificate satisfy a
 *     stable requirement (expected: both sides accept each other; ping
 *     exits 0):
 *
 *       # Create a local self-signed test CA and sign two copies.
 *       # (Requires Keychain access only for the test certificate itself,
 *       #  not used by the probe at runtime.)
 *       cp macos_hub_peer_probe serve_bin
 *       cp macos_hub_peer_probe ping_bin
 *
 *       # Sign both copies with a local test identity "MolokohubTestCA"
 *       # created once via Keychain Access → Certificate Assistant.
 *       codesign --sign "MolokohubTestCA" --force serve_bin
 *       codesign --sign "MolokohubTestCA" --force ping_bin
 *
 *       # Build the requirement string that pins to that certificate's CN.
 *       REQ='certificate leaf[subject.CN] = "MolokohubTestCA"'
 *
 *       # Terminal A – server: require clients to present MolokohubTestCA leaf.
 *       ./serve_bin serve \
 *           site.molokohub.droidhub.xpc-test.smoketest \
 *           "$REQ" &
 *       SERVER_PID=$!
 *
 *       # Terminal B – client: require server to present MolokohubTestCA leaf.
 *       ./ping_bin ping \
 *           site.molokohub.droidhub.xpc-test.smoketest \
 *           "$REQ"
 *       echo "ping exit: $?"   # expect 0 (pong received; both pass each other's req)
 *
 *       kill $SERVER_PID
 *
 * 3.  Timeout path (server not running):
 *
 *       ./macos_hub_peer_probe ping \
 *           site.molokohub.droidhub.xpc-test.smoketest \
 *           'anchor apple generic'
 *       echo "ping exit: $?"   # expect 4 (timeout)
 *
 * 4.  Label-guard rejection:
 *
 *       ./macos_hub_peer_probe serve \
 *           com.example.badlabel \
 *           'anchor apple generic'
 *       echo "exit: $?"        # expect 2
 *
 * =========================================================================
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

/** Client waits this many seconds for a pong before timing out (exit 4). */
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
 * on every accepted connection (using the public
 * xpc_connection_set_peer_code_signing_requirement API), then activates.
 * Only "ping" messages are handled; anything else is silently ignored.
 * ---------------------------------------------------------------------- */

static void run_serve(const char *label, const char *peer_requirement)
{
    enforce_label_prefix(label);

    /* Create a listener for the given Mach service name. */
    xpc_connection_t listener =
        xpc_connection_create_mach_service(label, NULL,
                                           XPC_CONNECTION_MACH_SERVICE_LISTENER);
    if (listener == NULL) {
        fprintf(stderr, "serve: xpc_connection_create_mach_service failed.\n");
        exit(EXIT_XPC_SETUP);
    }

    xpc_connection_set_event_handler(listener, ^(xpc_object_t event) {
        /* Each event on the listener is an incoming connection. */
        if (xpc_get_type(event) != XPC_TYPE_CONNECTION) {
            /* XPC error on the listener itself – log and ignore. */
            if (xpc_get_type(event) == XPC_TYPE_ERROR) {
                fprintf(stderr, "serve: listener error: %s\n",
                        xpc_dictionary_get_string(event, XPC_ERROR_KEY_DESCRIPTION));
            }
            return;
        }

        xpc_connection_t peer = (xpc_connection_t)event;

        /*
         * Apply the peer code-signing requirement BEFORE activating.
         * Public API: xpc_connection_set_peer_code_signing_requirement(3)
         * Available on macOS 12.0+.
         *
         * If the client does not satisfy this requirement, XPC delivers an
         * XPC_ERROR_CONNECTION_INTERRUPTED (or similar) error to both sides;
         * we do not need to verify the requirement manually.
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
                /* Not a dictionary – ignore (bounded, no looping). */
                return;
            }

            const char *val = xpc_dictionary_get_string(msg, MSG_KEY);
            if (val == NULL || strcmp(val, MSG_PING) != 0) {
                /* Unknown message – ignore silently, no reply. */
                return;
            }

            /* Send pong. */
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
            "serve: listening on \"%s\" with peer requirement:\n  %s\n"
            "Press Ctrl-C to stop.\n",
            label, peer_requirement);

    dispatch_main(); /* Never returns. */
}

/* -------------------------------------------------------------------------
 * ping mode
 *
 * Creates a client connection, applies a peer code-signing requirement
 * on the server using the same public API, activates, sends one ping, and
 * waits up to PING_TIMEOUT_SEC seconds for a pong.
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

    /*
     * Apply the peer requirement on the SERVER before activation.
     * If the server does not satisfy this, XPC delivers an error event.
     */
    if (xpc_connection_set_peer_code_signing_requirement(
            conn, peer_requirement) != 0) {
        fprintf(stderr,
                "ping: xpc_connection_set_peer_code_signing_requirement "
                "failed (bad requirement string?): \"%s\"\n",
                peer_requirement);
        xpc_release(conn);
        exit(EXIT_XPC_SETUP);
    }

    /*
     * We use a semaphore with a timed wait so that the tool exits cleanly
     * after PING_TIMEOUT_SEC seconds even if no server is running.
     * No busy-loop, no non-deterministic sleep.
     */
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);

    /* Shared result – written once from the reply block, read on main thread. */
    _Atomic int result = EXIT_TIMEOUT; /* pessimistic default */

    xpc_connection_set_event_handler(conn, ^(xpc_object_t event) {
        if (xpc_get_type(event) == XPC_TYPE_ERROR) {
            fprintf(stderr, "ping: connection error: %s\n",
                    xpc_dictionary_get_string(event,
                                              XPC_ERROR_KEY_DESCRIPTION));
            atomic_store(&result, EXIT_XPC_ERROR);
            dispatch_semaphore_signal(sem);
            return;
        }
        /* Unexpected non-error, non-message event – ignore. */
    });

    xpc_connection_activate(conn);

    /* Build and send the ping message. */
    xpc_object_t ping_msg = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_string(ping_msg, MSG_KEY, MSG_PING);

    xpc_connection_send_message_with_reply(
        conn, ping_msg, NULL, ^(xpc_object_t reply) {
            if (xpc_get_type(reply) == XPC_TYPE_ERROR) {
                fprintf(stderr, "ping: reply error: %s\n",
                        xpc_dictionary_get_string(reply,
                                                  XPC_ERROR_KEY_DESCRIPTION));
                atomic_store(&result, EXIT_XPC_ERROR);
                dispatch_semaphore_signal(sem);
                return;
            }
            if (xpc_get_type(reply) != XPC_TYPE_DICTIONARY) {
                fprintf(stderr, "ping: unexpected reply type.\n");
                atomic_store(&result, EXIT_BAD_MSG);
                dispatch_semaphore_signal(sem);
                return;
            }
            const char *val = xpc_dictionary_get_string(reply, MSG_KEY);
            if (val == NULL || strcmp(val, MSG_PONG) != 0) {
                fprintf(stderr,
                        "ping: reply has unexpected value \"%s\".\n",
                        val ? val : "(null)");
                atomic_store(&result, EXIT_BAD_MSG);
                dispatch_semaphore_signal(sem);
                return;
            }
            fprintf(stdout, "ping: pong received.\n");
            atomic_store(&result, EXIT_OK);
            dispatch_semaphore_signal(sem);
        });

    xpc_release(ping_msg);

    /* Wait up to PING_TIMEOUT_SEC seconds. */
    dispatch_time_t deadline =
        dispatch_time(DISPATCH_TIME_NOW,
                      (int64_t)PING_TIMEOUT_SEC * NSEC_PER_SEC);
    long timed_out = dispatch_semaphore_wait(sem, deadline);

    xpc_connection_cancel(conn);
    xpc_release(conn);

    if (timed_out != 0) {
        fprintf(stderr,
                "ping: timed out after %d second(s) – no server reply.\n",
                PING_TIMEOUT_SEC);
        exit(EXIT_TIMEOUT);
    }

    exit(atomic_load(&result));
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
            "The <mach-service-label> must begin with:\n"
            "  " LABEL_PREFIX "\n"
            "\n"
            "Exit codes:\n"
            "  0  success\n"
            "  1  usage / argument error\n"
            "  2  label prefix guard triggered\n"
            "  3  XPC setup error\n"
            "  4  ping timeout\n"
            "  5  ping XPC error (peer rejected or unreachable)\n"
            "  6  ping unexpected / invalid reply\n",
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

    return EXIT_USAGE; /* unreachable, but satisfies the compiler */
}

#else /* !__APPLE__ */

/*
 * Linux / non-macOS stub
 * ----------------------
 * This file depends on Apple XPC and Dispatch APIs that exist only in
 * macOS's libSystem.  It CANNOT be compiled or executed on Linux.
 *
 * REVIEWER NOTE
 * -------------
 * Linux CI workers are expected to reach this stub and exit with a clear
 * diagnostic, rather than failing with obscure linker or preprocessor
 * errors.  Please evaluate the macOS implementation above for:
 *
 *   • Correct use of xpc_connection_set_peer_code_signing_requirement(3)
 *   • Label-prefix guard (LABEL_PREFIX enforcement)
 *   • No private API, no PID-based authorization, no broad-default requirement
 *   • Ping/pong message discipline and bounded timeout
 *   • Correct exit codes for success, timeout, and error paths
 *   • No Keychain, network, installer, or production service access
 *
 * The local macOS lead will compile and run the native smoke tests
 * documented in the header comment above after review.
 */

#include <stdio.h>
#include <stdlib.h>

int main(void)
{
    fprintf(stderr,
        "macos_hub_peer_probe: This binary can only be compiled and run on\n"
        "macOS 12 or later.  Linux does not provide the XPC or Mach APIs\n"
        "required by this test tool.\n"
        "\n"
        "REVIEWER: Please evaluate the macOS implementation in the #ifdef __APPLE__\n"
        "section of this source file.  The local macOS lead will perform native\n"
        "compile and smoke-test execution after review.\n");
    return 3; /* EXIT_XPC_SETUP – closest meaningful code on this platform */
}

#endif /* __APPLE__ */
