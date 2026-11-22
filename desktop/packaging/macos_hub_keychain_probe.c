/*
 * macos_hub_keychain_probe.c
 *
 * TEST-ONLY diagnostic for macOS XPC + Security Keychain smoke tests.
 * NOT production code.  Extends the peer-probe ping/pong pattern with a
 * server-side Keychain lookup restricted to a throwaway test Keychain file.
 *
 * Mutual peer code-signing requirement on XPC (same as macos_hub_peer_probe.c).
 * Server replies "pong" ONLY when a hardcoded synthetic service/account exists
 * in the explicitly opened test Keychain and carries the expected fixed payload.
 *
 * Keychain safety constraints
 * ---------------------------
 *   - Opens only the keychain at the caller-supplied path (must resolve under
 *     /private/tmp/<process-name>/ inside the process's own explicit directory).
 *   - kSecMatchSearchList is set to that single keychain object – no default
 *     or user login keychain is ever consulted as a fallback.
 *   - kSecUseAuthenticationUI = kSecUseAuthenticationUIFail – UI / agent
 *     interaction is disabled; missing or locked items fail closed.
 *   - Item data (kSecReturnData) is NOT returned; only kSecReturnAttributes is
 *     fetched.  The fixed expected label is compared; no secret bytes are
 *     logged or forwarded.
 *   - The keychain is never modified (no SecItemAdd / SecItemDelete).
 *   - No production keychain, token, or credential is accessed.
 *
 * Usage
 * -----
 *   serve <mach-service-label> <client-code-requirement> <keychain-path>
 *   ping  <mach-service-label> <server-code-requirement>
 *
 * <mach-service-label> MUST begin with the prefix enforced below.
 * <keychain-path>      Must be under /private/tmp/<process-name>/.
 *
 * The keychain must already contain a generic-password item with:
 *   service  = PROBE_SERVICE   ("site.molokohub.test-svc")
 *   account  = PROBE_ACCOUNT   ("probe-account")
 *   label    = PROBE_LABEL     ("droidhub-keychain-probe-ok")
 *   password = any (not inspected)
 *
 * Exit codes
 * ----------
 *   0  success
 *   1  usage / argument error
 *   2  label prefix guard triggered
 *   3  XPC setup error
 *   4  ping timeout
 *   5  ping XPC error
 *   6  ping unexpected / invalid reply
 *   7  keychain path guard triggered
 *   8  keychain open / lookup error
 *
 * Compile (macOS 12+, Xcode CLT)
 * --------------------------------
 *   clang -std=c17 -Wall -Wextra -Wpedantic -Werror \
 *         -fblocks -mmacosx-version-min=12.0 \
 *         -framework Security \
 *         -o macos_hub_keychain_probe \
 *         macos_hub_keychain_probe.c
 *
 * LINUX LIMITATION
 * ----------------
 * XPC, Dispatch, and Security.framework APIs exist only on macOS.
 * Linux CI reaches the stub at the bottom and exits with a diagnostic.
 */

#ifdef __APPLE__

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>

#include <dispatch/dispatch.h>
#include <xpc/xpc.h>
#include <Security/Security.h>

/* -------------------------------------------------------------------------
 * Constants
 * ---------------------------------------------------------------------- */

#define LABEL_PREFIX   "site.molokohub.droidhub.xpc-test."
#define PATH_PREFIX    "/private/tmp/"

/* Hardcoded synthetic service identity – no production data. */
#define PROBE_SERVICE  "site.molokohub.test-svc"
#define PROBE_ACCOUNT  "probe-account"
#define PROBE_LABEL    "droidhub-keychain-probe-ok"

#define MSG_KEY        "msg"
#define MSG_PING       "ping"
#define MSG_PONG       "pong"

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
    EXIT_PATH_GUARD   = 7,
    EXIT_KEYCHAIN     = 8,
};

/* -------------------------------------------------------------------------
 * Guards
 * ---------------------------------------------------------------------- */

static void enforce_label_prefix(const char *label)
{
    if (strncmp(label, LABEL_PREFIX, strlen(LABEL_PREFIX)) != 0) {
        fprintf(stderr,
            "ERROR: label \"%s\" does not begin with required prefix \"%s\".\n",
            label, LABEL_PREFIX);
        exit(EXIT_LABEL_GUARD);
    }
}

static void enforce_path_prefix(const char *path)
{
    if (strncmp(path, PATH_PREFIX, strlen(PATH_PREFIX)) != 0) {
        fprintf(stderr,
            "ERROR: keychain path \"%s\" must be under \"%s\".\n",
            path, PATH_PREFIX);
        exit(EXIT_PATH_GUARD);
    }
}

/* -------------------------------------------------------------------------
 * keychain_probe
 *
 * Opens the throwaway keychain at 'kc_path', restricts the search to that
 * single keychain, disables UI interaction, and verifies that the expected
 * synthetic item (service/account/label) is present.  Item data is never
 * fetched or logged.  Returns 1 on success, 0 on any failure.
 * ---------------------------------------------------------------------- */

static int keychain_probe(const char *kc_path)
{
    SecKeychainRef kc = NULL;
    OSStatus st = SecKeychainOpen(kc_path, &kc);
    if (st != errSecSuccess || kc == NULL) {
        fprintf(stderr, "keychain_probe: SecKeychainOpen failed (%d).\n", (int)st);
        return 0;
    }

    CFArrayRef search_list = CFArrayCreate(
        NULL, (const void **)&kc, 1, &kCFTypeArrayCallBacks);
    if (search_list == NULL) {
        CFRelease(kc);
        return 0;
    }

    CFStringRef svc = CFStringCreateWithCString(
        NULL, PROBE_SERVICE, kCFStringEncodingUTF8);
    CFStringRef acct = CFStringCreateWithCString(
        NULL, PROBE_ACCOUNT, kCFStringEncodingUTF8);
    CFStringRef expected_label = CFStringCreateWithCString(
        NULL, PROBE_LABEL, kCFStringEncodingUTF8);

    int ok = 0;

    if (svc && acct && expected_label) {
        CFDictionaryRef query = (__bridge_retained CFDictionaryRef)@{
            /* scope */
            (__bridge id)kSecClass               : (__bridge id)kSecClassGenericPassword,
            (__bridge id)kSecAttrService         : (__bridge id)svc,
            (__bridge id)kSecAttrAccount         : (__bridge id)acct,
            /* restrict to our throwaway keychain only */
            (__bridge id)kSecMatchSearchList     : (__bridge id)search_list,
            /* disable UI/agent interaction – fail closed */
            (__bridge id)kSecUseAuthenticationUI : (__bridge id)kSecUseAuthenticationUIFail,
            /* return attributes only; never return secret bytes */
            (__bridge id)kSecReturnAttributes    : @YES,
            (__bridge id)kSecMatchLimit          : (__bridge id)kSecMatchLimitOne,
        };

        CFTypeRef result = NULL;
        st = SecItemCopyMatching(query, &result);
        CFRelease(query);

        if (st == errSecSuccess && result != NULL &&
            CFGetTypeID(result) == CFDictionaryGetTypeID()) {
            CFStringRef label = CFDictionaryGetValue(
                (CFDictionaryRef)result, kSecAttrLabel);
            /* Compare label; never log item data. */
            if (label != NULL &&
                CFStringCompare(label, expected_label, 0) == kCFCompareEqualTo) {
                ok = 1;
            } else {
                fprintf(stderr, "keychain_probe: label mismatch.\n");
            }
        } else {
            fprintf(stderr, "keychain_probe: SecItemCopyMatching failed (%d).\n", (int)st);
        }

        if (result) CFRelease(result);
    }

    CFRelease(search_list);
    CFRelease(kc);
    if (svc)             CFRelease(svc);
    if (acct)            CFRelease(acct);
    if (expected_label)  CFRelease(expected_label);
    return ok;
}

/* -------------------------------------------------------------------------
 * serve mode
 * ---------------------------------------------------------------------- */

static void run_serve(const char *label,
                      const char *peer_requirement,
                      const char *kc_path)
{
    enforce_label_prefix(label);
    enforce_path_prefix(kc_path);

    xpc_connection_t listener =
        xpc_connection_create_mach_service(label, NULL,
                                           XPC_CONNECTION_MACH_SERVICE_LISTENER);
    if (listener == NULL) {
        fprintf(stderr, "serve: xpc_connection_create_mach_service failed.\n");
        exit(EXIT_XPC_SETUP);
    }

    /* Capture kc_path as a heap copy so the block owns it safely. */
    char *kc = strdup(kc_path);
    if (kc == NULL) { perror("strdup"); exit(EXIT_XPC_SETUP); }

    xpc_connection_set_event_handler(listener, ^(xpc_object_t event) {
        if (xpc_get_type(event) != XPC_TYPE_CONNECTION) {
            if (xpc_get_type(event) == XPC_TYPE_ERROR)
                fprintf(stderr, "serve: listener error: %s\n",
                        xpc_dictionary_get_string(event, XPC_ERROR_KEY_DESCRIPTION));
            return;
        }

        xpc_connection_t peer = (xpc_connection_t)event;

        if (xpc_connection_set_peer_code_signing_requirement(
                peer, peer_requirement) != 0) {
            fprintf(stderr, "serve: bad peer requirement string.\n");
            xpc_connection_cancel(peer);
            return;
        }

        xpc_connection_set_event_handler(peer, ^(xpc_object_t msg) {
            if (xpc_get_type(msg) != XPC_TYPE_DICTIONARY) return;

            const char *val = xpc_dictionary_get_string(msg, MSG_KEY);
            if (val == NULL || strcmp(val, MSG_PING) != 0) return;

            /* Keychain gate: reply pong only on successful probe. */
            xpc_object_t reply = xpc_dictionary_create_reply(msg);
            if (reply == NULL) return;

            if (keychain_probe(kc)) {
                xpc_dictionary_set_string(reply, MSG_KEY, MSG_PONG);
            } else {
                /* Fail closed: send an error string, not pong. */
                xpc_dictionary_set_string(reply, MSG_KEY, "keychain-fail");
            }
            xpc_connection_send_message(peer, reply);
            xpc_release(reply);
        });

        xpc_connection_activate(peer);
    });

    xpc_connection_activate(listener);
    fprintf(stdout, "serve: listening on \"%s\" (keychain: %s)\nPress Ctrl-C to stop.\n",
            label, kc_path);
    dispatch_main();
}

/* -------------------------------------------------------------------------
 * ping mode  (unchanged from peer_probe)
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

    if (xpc_connection_set_peer_code_signing_requirement(conn, peer_requirement) != 0) {
        fprintf(stderr, "ping: bad peer requirement string.\n");
        xpc_release(conn);
        exit(EXIT_XPC_SETUP);
    }

    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    _Atomic int *result    = malloc(sizeof(_Atomic int));
    _Atomic int *signalled = malloc(sizeof(_Atomic int));
    if (!result || !signalled) { perror("malloc"); exit(EXIT_XPC_SETUP); }
    atomic_store(result,    EXIT_TIMEOUT);
    atomic_store(signalled, 0);

    xpc_connection_set_event_handler(conn, ^(xpc_object_t event) {
        if (xpc_get_type(event) != XPC_TYPE_ERROR) return;
        fprintf(stderr, "ping: connection error: %s\n",
                xpc_dictionary_get_string(event, XPC_ERROR_KEY_DESCRIPTION));
        int exp = 0;
        if (atomic_compare_exchange_strong(signalled, &exp, 1)) {
            atomic_store(result, EXIT_XPC_ERROR);
            dispatch_semaphore_signal(sem);
        }
    });

    xpc_connection_activate(conn);

    xpc_object_t ping_msg = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_string(ping_msg, MSG_KEY, MSG_PING);

    xpc_connection_send_message_with_reply(conn, ping_msg, NULL, ^(xpc_object_t reply) {
        int code;
        if (xpc_get_type(reply) == XPC_TYPE_ERROR) {
            fprintf(stderr, "ping: reply error: %s\n",
                    xpc_dictionary_get_string(reply, XPC_ERROR_KEY_DESCRIPTION));
            code = EXIT_XPC_ERROR;
        } else if (xpc_get_type(reply) != XPC_TYPE_DICTIONARY) {
            fprintf(stderr, "ping: unexpected reply type.\n");
            code = EXIT_BAD_MSG;
        } else {
            const char *v = xpc_dictionary_get_string(reply, MSG_KEY);
            if (v != NULL && strcmp(v, MSG_PONG) == 0) {
                fprintf(stdout, "ping: pong received.\n");
                code = EXIT_OK;
            } else {
                fprintf(stderr, "ping: unexpected reply \"%s\".\n", v ? v : "(null)");
                code = EXIT_BAD_MSG;
            }
        }
        int exp = 0;
        if (atomic_compare_exchange_strong(signalled, &exp, 1)) {
            atomic_store(result, code);
            dispatch_semaphore_signal(sem);
        }
    });

    xpc_release(ping_msg);

    dispatch_time_t deadline =
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)PING_TIMEOUT_SEC * NSEC_PER_SEC);
    long timed_out = dispatch_semaphore_wait(sem, deadline);

    xpc_connection_cancel(conn);
    xpc_release(conn);

    int code = timed_out ? EXIT_TIMEOUT : atomic_load(result);
    if (timed_out)
        fprintf(stderr, "ping: timed out after %d second(s).\n", PING_TIMEOUT_SEC);
    exit(code);
}

/* -------------------------------------------------------------------------
 * Entry point
 * ---------------------------------------------------------------------- */

int main(int argc, char *argv[])
{
    if (argc == 4 && strcmp(argv[1], "ping") == 0) {
        run_ping(argv[2], argv[3]);
    } else if (argc == 5 && strcmp(argv[1], "serve") == 0) {
        run_serve(argv[2], argv[3], argv[4]);
    } else {
        fprintf(stderr,
            "Usage:\n"
            "  %s serve <mach-service-label> <client-code-requirement> <keychain-path>\n"
            "  %s ping  <mach-service-label> <server-code-requirement>\n"
            "\n"
            "Label must begin with: " LABEL_PREFIX "\n"
            "Keychain path must be under: " PATH_PREFIX "\n"
            "\n"
            "Exit codes: 0 ok  1 usage  2 label-guard  3 xpc-setup\n"
            "            4 timeout  5 xpc-error  6 bad-reply\n"
            "            7 path-guard  8 keychain-error\n",
            argv[0], argv[0]);
        return EXIT_USAGE;
    }
    return EXIT_USAGE; /* unreachable */
}

#else /* !__APPLE__ */

/*
 * Linux / non-macOS stub
 * ----------------------
 * XPC, Dispatch, and Security.framework exist only on macOS.
 * Linux CI exits here with a clear diagnostic.
 */

#include <stdio.h>
#include <stdlib.h>

int main(void)
{
    fprintf(stderr,
        "macos_hub_keychain_probe: requires macOS 12+ "
        "(XPC/Security APIs unavailable on Linux).\n"
        "Evaluate the #ifdef __APPLE__ section for correctness.\n");
    return 3; /* EXIT_XPC_SETUP */
}

#endif /* __APPLE__ */
