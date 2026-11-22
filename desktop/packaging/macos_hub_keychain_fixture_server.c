/*
 * macos_hub_keychain_fixture_server.c  –  TEST-ONLY Keychain fixture server.
 *
 * Usage: <binary> <test-mach-label> <client-requirement> <abs-keychain-path>
 *
 * Compile (macOS 12+, Xcode CLT):
 *   clang -std=c17 -Wall -Wextra -Wpedantic -Werror -fblocks \
 *         -mmacosx-version-min=12.0 -framework Security \
 *         -framework CoreFoundation \
 *         -o macos_hub_keychain_fixture_server \
 *         macos_hub_keychain_fixture_server.c
 *
 * Exit: 0 ok  1 usage  2 label-guard  3 xpc-setup  7 path-invalid  8 kc-fail
 *
 * LINUX: APIs are macOS-only; Linux CI reaches the stub below.
 */

#ifdef __APPLE__

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>

#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <dispatch/dispatch.h>
#include <xpc/xpc.h>

#define LABEL_PREFIX  "site.molokohub.droidhub.xpc-test."
#define KC_DIR_PREFIX "/private/tmp/droidhub-hub-kc-smoke."
#define KC_BASENAME   "fixture.keychain-db"
#define KC_SVC        "site.molokohub.test-svc"
#define KC_ACCOUNT    "probe-account"
#define MSG_KEY       "msg"
#define MSG_PING      "ping"
#define MSG_PONG      "pong"

static const uint8_t DUMMY[]  = "droidhub-synthetic-only";
static const size_t  DUMMY_LEN = sizeof(DUMMY) - 1;

enum { EX_OK=0, EX_USAGE=1, EX_GUARD=2, EX_XPC=3, EX_PATH=7, EX_KC=8 };

/* Reject symlinks, directories, non-canonical paths, bad ownership/perms. */
static void check_path(const char *p)
{
    if (p[0] != '/') { fprintf(stderr,"ERROR: not absolute\n"); exit(EX_PATH); }

    char res[PATH_MAX];
    if (!realpath(p, res) || strcmp(p, res) != 0) {
        fprintf(stderr,"ERROR: non-canonical path: %s\n", p); exit(EX_PATH);
    }
    if (strncmp(res, KC_DIR_PREFIX, strlen(KC_DIR_PREFIX)) != 0) {
        fprintf(stderr,"ERROR: must be under %s*/%s\n", KC_DIR_PREFIX, KC_BASENAME);
        exit(EX_PATH);
    }
    const char *b = strrchr(res, '/'); b = b ? b+1 : res;
    if (strcmp(b, KC_BASENAME) != 0) {
        fprintf(stderr,"ERROR: basename must be %s\n", KC_BASENAME); exit(EX_PATH);
    }

    struct stat st;
    if (lstat(res, &st)!=0 || !S_ISREG(st.st_mode) || st.st_uid!=getuid()) {
        fprintf(stderr,"ERROR: must be a regular file owned by caller\n"); exit(EX_PATH);
    }

    char dir[PATH_MAX]; snprintf(dir, sizeof(dir), "%s", res);
    char *sl = strrchr(dir, '/'); if (sl && sl!=dir) *sl='\0';
    struct stat ds;
    if (stat(dir,&ds)!=0 || !S_ISDIR(ds.st_mode) ||
        ds.st_uid!=getuid() || (ds.st_mode&0777)!=0700) {
        fprintf(stderr,"ERROR: dir must be 0700 owned by caller\n"); exit(EX_PATH);
    }
}

/* Open KC, query generic password in-memory, compare to fixed dummy bytes. */
static bool probe_kc(const char *kc_path)
{
    if (SecKeychainSetUserInteractionAllowed(false) != errSecSuccess) return false;

    SecKeychainRef kc = NULL;
    if (SecKeychainOpen(kc_path, &kc) != errSecSuccess || !kc) return false;

    const void *kc_items[1] = { kc };
    CFArrayRef sl = CFArrayCreate(kCFAllocatorDefault, kc_items, 1,
                                  &kCFTypeArrayCallBacks);
    CFStringRef svc  = CFStringCreateWithCString(NULL, KC_SVC,     kCFStringEncodingUTF8);
    CFStringRef acct = CFStringCreateWithCString(NULL, KC_ACCOUNT, kCFStringEncodingUTF8);
    if (!sl || !svc || !acct) {
        if (sl)   CFRelease(sl);
        if (svc)  CFRelease(svc);
        if (acct) CFRelease(acct);
        CFRelease(kc); return false;
    }

    const void *keys[5] = { kSecClass, kSecAttrService, kSecAttrAccount,
                             kSecMatchSearchList, kSecReturnData };
    const void *vals[5] = { kSecClassGenericPassword, svc, acct,
                             sl, kCFBooleanTrue };
    CFDictionaryRef q = CFDictionaryCreate(kCFAllocatorDefault,
                                           keys, vals, 5,
                                           &kCFTypeDictionaryKeyCallBacks,
                                           &kCFTypeDictionaryValueCallBacks);
    CFRelease(svc); CFRelease(acct); CFRelease(sl); CFRelease(kc);
    if (!q) return false;

    CFTypeRef res = NULL;
    OSStatus err = SecItemCopyMatching(q, &res);
    CFRelease(q);
    if (err != errSecSuccess || !res) return false;

    bool ok = false;
    if (CFGetTypeID(res) == CFDataGetTypeID()) {
        CFDataRef d = (CFDataRef)res;
        ok = (size_t)CFDataGetLength(d) == DUMMY_LEN &&
             memcmp(CFDataGetBytePtr(d), DUMMY, DUMMY_LEN) == 0;
    }
    CFRelease(res);
    return ok;
}

static void enforce_prefix(const char *label)
{
    if (strncmp(label, LABEL_PREFIX, strlen(LABEL_PREFIX)) != 0) {
        fprintf(stderr,"ERROR: label must begin with %s\n", LABEL_PREFIX);
        exit(EX_GUARD);
    }
}

static void run_serve(const char *label, const char *req, const char *kc_path)
{
    enforce_prefix(label);
    check_path(kc_path);
    if (!probe_kc(kc_path)) {
        fprintf(stderr,"ERROR: keychain probe failed – dummy secret mismatch\n");
        exit(EX_KC);
    }

    xpc_connection_t ln =
        xpc_connection_create_mach_service(label, NULL,
                                           XPC_CONNECTION_MACH_SERVICE_LISTENER);
    if (!ln) { fprintf(stderr,"ERROR: create listener failed\n"); exit(EX_XPC); }

    xpc_connection_set_event_handler(ln, ^(xpc_object_t ev) {
        if (xpc_get_type(ev) != XPC_TYPE_CONNECTION) return;
        xpc_connection_t peer = (xpc_connection_t)ev;
        if (xpc_connection_set_peer_code_signing_requirement(peer, req) != 0) {
            xpc_connection_cancel(peer); return;
        }
        xpc_connection_set_event_handler(peer, ^(xpc_object_t msg) {
            if (xpc_get_type(msg) != XPC_TYPE_DICTIONARY) return;
            const char *v = xpc_dictionary_get_string(msg, MSG_KEY);
            if (!v || strcmp(v, MSG_PING) != 0) return;
            xpc_object_t rep = xpc_dictionary_create_reply(msg);
            if (rep) {
                xpc_dictionary_set_string(rep, MSG_KEY, MSG_PONG);
                xpc_connection_send_message(peer, rep);
                xpc_release(rep);
            }
        });
        xpc_connection_activate(peer);
    });

    xpc_connection_activate(ln);
    fprintf(stdout, "keychain-fixture-server: listening on \"%s\"\n", label);
    dispatch_main();
}

int main(int argc, char *argv[])
{
    if (argc != 4) {
        fprintf(stderr,
            "Usage: %s <test-mach-label> <client-requirement>"
            " <absolute-keychain-path>\n"
            "Label prefix: " LABEL_PREFIX "\n"
            "Path form:    " KC_DIR_PREFIX "<random>/" KC_BASENAME "\n",
            argv[0]);
        return EX_USAGE;
    }
    run_serve(argv[1], argv[2], argv[3]);
    return EX_USAGE;
}

#else /* !__APPLE__ */

/*
 * Linux stub – Security/XPC/Mach APIs are macOS-only.
 * Linux CI worker reports native run unavailable; macOS lead performs native
 * compile and launchd smoke test (wrong/missing dummy, unauthorized client).
 */
#include <stdio.h>
#include <stdlib.h>
int main(void)
{
    fprintf(stderr,
        "macos_hub_keychain_fixture_server: requires macOS 12+ "
        "(Security/XPC/Mach APIs unavailable on Linux).\n"
        "Evaluate the #ifdef __APPLE__ section for correctness.\n");
    return 3;
}

#endif /* __APPLE__ */
