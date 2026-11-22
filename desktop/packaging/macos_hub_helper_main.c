/*
 * macos_hub_helper_main.c
 *
 * Native fixed entry point for the macOS Hub helper.
 *
 * ==========================================================================
 * Usage
 * ==========================================================================
 *
 *   macos_hub_helper               -- Serve XPC clients (normal operation).
 *   macos_hub_helper --authorize   -- Check/authorize the Keychain token
 *                                     interactively (human CLI only).
 *
 *   No other commands are accepted; unknown arguments exit with code 2.
 *
 * ==========================================================================
 * Default mode (no arguments)
 * ==========================================================================
 *
 *   Calls:
 *     dh_hub_serve(
 *         "site.molokohub.droidhub.hub-helper.v1",
 *         client_requirement,
 *         dh_hub_read_inventory,
 *         NULL
 *     )
 *
 *   client_requirement is derived at build time from two fixed constants:
 *     - DH_HUB_CLIENT_IDENTIFIER  "site.molokohub.droidhub"  (hardcoded)
 *     - DH_HUB_SIGNING_CERT_SHA1  must be defined as a string literal
 *       containing exactly 40 hexadecimal characters (0-9, a-f, A-F).
 *       Validated before dh_hub_serve is called; binary exits with code 2
 *       on a malformed macro value.
 *
 *   Requirement string format:
 *     identifier "site.molokohub.droidhub" and certificate leaf = H"<hex>"
 *
 *   There is no runtime override of either the identifier or the hash.
 *
 * ==========================================================================
 * --authorize mode
 * ==========================================================================
 *
 *   Calls dh_hub_read_keychain_token_interactive() to verify that the
 *   Keychain token is present and non-empty.  One macOS prompt may appear
 *   (intentional; this is a human-triggered path only).
 *
 *   Exit codes:
 *     0  Token is readable and non-empty.
 *     1  Token is unavailable (not found, auth failure, empty, etc.).
 *
 *   The token is NOT printed, logged, or transmitted anywhere.  After the
 *   check the in-process buffer is zeroed with a volatile loop so that the
 *   sensitive bytes are not retained in memory longer than necessary.
 *
 *   This path MUST NOT be called from XPC providers or automated contexts.
 *
 * ==========================================================================
 * Build requirements
 * ==========================================================================
 *
 *   -DH_HUB_SIGNING_CERT_SHA1="<40 hex chars>"    (required)
 *
 *   Frameworks:
 *     -framework Security
 *     -framework CoreFoundation
 *
 *   Linked translation units:
 *     macos_hub_helper_keychain.c   (provides dh_hub_read_keychain_token,
 *                                    dh_hub_read_keychain_token_interactive)
 *
 *   External symbols resolved by the caller's link unit (not declared here
 *   beyond what is strictly needed):
 *     dh_hub_serve          -- XPC serving entry point
 *     dh_hub_read_inventory -- inventory callback passed to dh_hub_serve
 *
 *   Example link (clang, macOS 11+):
 *     clang -arch arm64 -arch x86_64 \
 *           -mmacosx-version-min=11.0 \
 *           -framework Security -framework CoreFoundation \
 *           -DDH_HUB_SIGNING_CERT_SHA1="<40 hex chars>" \
 *           macos_hub_helper_main.c macos_hub_helper_keychain.c \
 *           <hub_serve_object> \
 *           -o macos_hub_helper
 *
 * ==========================================================================
 * WARNING: dev-macro / production-build mixing
 * ==========================================================================
 *
 *   If macos_hub_helper_keychain.c was compiled with -DDH_HUB_TEST_FIXTURE
 *   the resulting object reads the TEST fixture Keychain item, NOT the
 *   production item.  Linking such an object with this main produces a TEST
 *   BINARY ONLY.  Test binaries must be clearly labelled and must never be
 *   deployed to production Keychain environments.  There is no runtime
 *   detection of this mismatch; build infrastructure must enforce separation.
 *
 * ==========================================================================
 */

#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

/*
 * DH_HUB_SIGNING_CERT_SHA1 must be defined at compile time as a string
 * literal containing exactly 40 hexadecimal characters.
 *
 * Example:
 *   -DDH_HUB_SIGNING_CERT_SHA1="aabbccdd..."
 */
#ifndef DH_HUB_SIGNING_CERT_SHA1
#  error "DH_HUB_SIGNING_CERT_SHA1 must be defined at compile time (40 hex chars)"
#endif

/* Fixed client identifier — no runtime override. */
#define DH_HUB_CLIENT_IDENTIFIER "site.molokohub.droidhub"

/* XPC service label advertised by this helper. */
#define DH_HUB_SERVICE_LABEL "site.molokohub.droidhub.hub-helper.v1"

/* Token buffer size (matches DH_HUB_TOKEN_MAX in keychain translation unit). */
#define DH_HUB_TOKEN_MAX ((size_t)4096)

/* -------------------------------------------------------------------------
 * External symbols
 * ------------------------------------------------------------------------- */

/*
 * dh_hub_read_keychain_token
 *
 * Provided by macos_hub_helper_keychain.c.
 * Reads the Keychain token without any user-interaction prompt.
 * Used by the XPC-provider path (dh_hub_read_inventory).
 */
extern int dh_hub_read_keychain_token(void *out, size_t capacity,
                                      size_t *written);

/*
 * dh_hub_read_keychain_token_interactive
 *
 * Provided by macos_hub_helper_keychain.c.
 * Reads the Keychain token, allowing one macOS prompt if needed.
 * Used ONLY by the --authorize path.
 */
extern int dh_hub_read_keychain_token_interactive(void *out, size_t capacity,
                                                  size_t *written);

/*
 * dh_hub_read_inventory
 *
 * Inventory callback matching the ABI expected by dh_hub_serve:
 *   int(void *out, void *ctx, size_t capacity, size_t *written)
 *
 * Provided by the hub-serve link unit.  Passed directly to dh_hub_serve;
 * this translation unit only takes its address.
 */
extern int dh_hub_read_inventory(void *out, void *ctx,
                                 size_t capacity, size_t *written);

/*
 * dh_hub_serve
 *
 * XPC serving entry point.
 *
 * Parameters:
 *   label               -- NUL-terminated XPC service label string.
 *   client_requirement  -- NUL-terminated Code Signing requirement string.
 *   read_cb             -- Inventory reader callback (ABI: int(void*,void*,size_t,size_t*)).
 *   ctx                 -- Opaque context pointer passed through to read_cb (NULL here).
 *
 * Does not return in normal operation (runs the XPC listener run loop).
 */
extern void dh_hub_serve(const char *label,
                         const char *client_requirement,
                         int (*read_cb)(void *, void *, size_t, size_t *),
                         void *ctx);

/* -------------------------------------------------------------------------
 * Compile-time hex validation helpers
 * ------------------------------------------------------------------------- */

/*
 * dh_is_hex_char — returns 1 if c is a valid lowercase or uppercase hex digit.
 */
static int dh_is_hex_char(char c)
{
    return (c >= '0' && c <= '9') ||
           (c >= 'a' && c <= 'f') ||
           (c >= 'A' && c <= 'F');
}

/*
 * dh_validate_sha1_hex
 *
 * Validates that `s` is exactly 40 hexadecimal characters followed by a NUL.
 * Returns 1 on success, 0 on failure.
 */
static int dh_validate_sha1_hex(const char *s)
{
    size_t i;
    for (i = 0; i < 40; i++) {
        if (!dh_is_hex_char(s[i])) {
            return 0;
        }
    }
    return s[40] == '\0';
}

/* -------------------------------------------------------------------------
 * --authorize implementation
 * ------------------------------------------------------------------------- */

/*
 * dh_cmd_authorize
 *
 * Checks whether the Keychain token is present and non-empty using the
 * interactive reader (may show one macOS prompt).
 *
 * Returns:
 *   0  Token readable and non-empty.
 *   1  Token unavailable.
 *
 * The token is NOT exposed to any external channel.  The in-process buffer
 * is zeroed via a volatile loop before the function returns.
 */
static int dh_cmd_authorize(void)
{
    /*
     * Stack-allocated buffer.  Declared volatile so the compiler cannot
     * elide the zeroing loop below (as an "unused store to dead variable").
     */
    volatile unsigned char buf[DH_HUB_TOKEN_MAX];
    size_t written = 0;
    int rc;

    rc = dh_hub_read_keychain_token_interactive(
            (void *)buf, sizeof(buf), &written);

    /*
     * Zero the buffer unconditionally via volatile pointer so the sensitive
     * bytes are not retained in memory after this function returns.
     * The cast through volatile unsigned char * prevents the compiler from
     * treating the stores as dead.
     */
    {
        volatile unsigned char *p = buf;
        size_t i;
        for (i = 0; i < sizeof(buf); i++) {
            p[i] = 0;
        }
    }

    if (rc != 0 || written == 0) {
        return 1;
    }
    return 0;
}

/* -------------------------------------------------------------------------
 * main
 * ------------------------------------------------------------------------- */

int main(int argc, char *argv[])
{
    /*
     * Validate the signing-cert SHA-1 hash at the earliest possible moment.
     * This is a compile-time constant; the check catches mis-defined macros
     * (wrong length, non-hex chars) and fails fast before any Keychain or
     * XPC activity.
     */
    static const char k_cert_sha1[] = DH_HUB_SIGNING_CERT_SHA1;

    if (!dh_validate_sha1_hex(k_cert_sha1)) {
        /*
         * Write to stderr so the operator sees the failure reason even when
         * stdout is redirected.  No token data is involved at this point.
         */
        static const char msg[] =
            "macos_hub_helper: fatal: DH_HUB_SIGNING_CERT_SHA1 is not "
            "exactly 40 hexadecimal characters\n";
        /* Use write() via stderr FILE* — no printf to avoid locale machinery. */
        (void)fwrite(msg, 1, sizeof(msg) - 1, stderr);
        return 2;
    }

    /* -----------------------------------------------------------------------
     * Parse arguments.  Exactly one optional argument is accepted.
     * ----------------------------------------------------------------------- */

    if (argc == 1) {
        /*
         * Default mode: start the XPC hub server.
         *
         * Build the client requirement string:
         *   identifier "site.molokohub.droidhub" and certificate leaf = H"<hex>"
         *
         * The requirement string is assembled in a fixed-size stack buffer.
         * Layout:
         *   identifier "<IDENT>" and certificate leaf = H"<40HEX>"
         *
         * Buffer sizing:
         *   "identifier \"" = 12
         *   DH_HUB_CLIENT_IDENTIFIER (24 chars max, known at compile time)
         *   "\" and certificate leaf = H\"" = 28
         *   40 hex chars
         *   "\"" + NUL = 2
         *   Total: 12 + 24 + 28 + 40 + 2 = 106; use 256 for safety.
         */
        char req[256];
        int req_len;

        /*
         * snprintf is safe here: the format string and all arguments are
         * compile-time constants or validated constants.  The buffer is
         * large enough (see sizing above); we still check the return value.
         */
        req_len = snprintf(req, sizeof(req),
                           "identifier \"%s\" and certificate leaf = H\"%s\"",
                           DH_HUB_CLIENT_IDENTIFIER,
                           k_cert_sha1);

        if (req_len <= 0 || (size_t)req_len >= sizeof(req)) {
            static const char msg[] =
                "macos_hub_helper: fatal: requirement string assembly failed\n";
            (void)fwrite(msg, 1, sizeof(msg) - 1, stderr);
            return 2;
        }

        /*
         * Hand control to dh_hub_serve.  Does not return in normal operation.
         */
        dh_hub_serve(DH_HUB_SERVICE_LABEL, req, dh_hub_read_inventory, NULL);

        /*
         * dh_hub_serve should not return.  If it does, treat it as an error.
         */
        return 1;
    }

    if (argc == 2 && strcmp(argv[1], "--authorize") == 0) {
        return dh_cmd_authorize();
    }

    /* Unknown invocation. */
    {
        static const char usage[] =
            "usage: macos_hub_helper [--authorize]\n"
            "  (no arguments)   Start the XPC hub server.\n"
            "  --authorize      Check/authorize the Keychain token (human CLI only).\n";
        (void)fwrite(usage, 1, sizeof(usage) - 1, stderr);
        return 2;
    }
}
