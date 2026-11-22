/*
 * macos_hub_helper_keychain.c
 *
 * Standalone native helper: read an existing login-Keychain token without UI.
 * This file is the only source file for this translation unit.  It has no
 * main(), produces no output, writes to no files, and touches no Keychain
 * entries other than reading the single item described below.
 *
 * ==========================================================================
 * ABI contract
 * ==========================================================================
 *
 *   int dh_hub_read_keychain_token(void *out, size_t capacity, size_t *written);
 *
 *   Return values
 *   -------------
 *     0   Success.  *written bytes have been placed in *out.
 *     1   Item not found, authorization failure, type mismatch, interaction
 *         could not be disabled, internal error, or any other failure.
 *         *written is always 0 on return code 1.
 *
 *   Guarantees
 *   ----------
 *   - No partial writes: if the token does not fit in `capacity` the call
 *     fails (returns 1, *written = 0).
 *   - Maximum data accepted from Keychain: 4096 bytes (DH_HUB_TOKEN_MAX).
 *     Data longer than this is rejected.
 *   - `out` is written only on success (return 0).
 *   - No NUL terminator is appended; the caller receives raw bytes.
 *   - No printing, logging, or credential output to any external channel.
 *   - Thread safety: Keychain user-interaction is serialised via XPC by the
 *     OS itself; calls from multiple threads are safe at the Security.framework
 *     level, but callers should serialise if they depend on the interaction-
 *     allowed flag being consistent across concurrent calls.
 *
 * ==========================================================================
 * Keychain item identity (production)
 * ==========================================================================
 *
 *   kSecClass            kSecClassGenericPassword
 *   kSecAttrService      "site.molokohub.droidhub.desktop.hub"
 *   kSecAttrAccount      "https://droidhub.molokohub.site:443/api/devices"
 *
 *   These match the values written by the desktop HubConnectionStore.
 *
 * ==========================================================================
 * Test-fixture item identity  (compile-time macro DH_HUB_TEST_FIXTURE)
 * ==========================================================================
 *
 *   Defining DH_HUB_TEST_FIXTURE at compile time switches the helper to a
 *   distinct, never-production item so that integration tests can inject a
 *   known token without touching the real Keychain entry.
 *
 *   Static constants (see below):
 *     DH_HUB_TEST_SERVICE  "site.molokohub.droidhub.desktop.hub.__test__"
 *     DH_HUB_TEST_ACCOUNT  "https://test.invalid/dh_hub_keychain_fixture"
 *
 *   These values are chosen to be:
 *     - structurally valid Keychain strings,
 *     - syntactically distinct from any real endpoint (RFC 2606 ".invalid"),
 *     - documented here so the fixture entry can be populated / deleted by
 *       test infrastructure without ambiguity.
 *
 *   Production builds NEVER read the fixture item; there is no runtime
 *   override path.
 *
 * ==========================================================================
 * Build notes
 * ==========================================================================
 *
 *   Frameworks required:
 *     -framework Security
 *     -framework CoreFoundation
 *
 *   Compile with -DDEPRECATION_SILENCE (or use the per-file pragma below)
 *   if the build system promotes deprecation warnings to errors.
 *   The deprecated SecKeychainGetUserInteractionAllowed /
 *   SecKeychainSetUserInteractionAllowed APIs are used intentionally: the
 *   modern replacements do not offer the same synchronous guarantee needed
 *   here.  Deprecation is suppressed narrowly around only those two calls.
 *
 *   Example invocation (clang, macOS 11+):
 *     clang -arch arm64 -arch x86_64 \
 *           -mmacosx-version-min=11.0 \
 *           -framework Security -framework CoreFoundation \
 *           -o macos_hub_helper_keychain.o \
 *           -c macos_hub_helper_keychain.c
 *
 *   For the test fixture:
 *     clang ... -DDH_HUB_TEST_FIXTURE -c macos_hub_helper_keychain.c
 */

#include <stddef.h>
#include <string.h>

#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>

/* -------------------------------------------------------------------------
 * Compile-time constants
 * ------------------------------------------------------------------------- */

/** Maximum number of bytes accepted from the Keychain data value. */
#define DH_HUB_TOKEN_MAX ((size_t)4096)

/* Production Keychain item coordinates, matching HubConnectionStore. */
#define DH_HUB_PROD_SERVICE "site.molokohub.droidhub.desktop.hub"
#define DH_HUB_PROD_ACCOUNT "https://droidhub.molokohub.site:443/api/devices"

/*
 * Test-fixture Keychain item coordinates.
 * Used only when DH_HUB_TEST_FIXTURE is defined at compile time.
 * The ".invalid" TLD (RFC 2606) guarantees no collision with any real host.
 * The "__test__" suffix in the service name makes the item easy to identify
 * and clean up in test teardown scripts.
 */
#define DH_HUB_TEST_SERVICE "site.molokohub.droidhub.desktop.hub.__test__"
#define DH_HUB_TEST_ACCOUNT "https://test.invalid/dh_hub_keychain_fixture"

/* Select active coordinates at compile time — no runtime branch. */
#if defined(DH_HUB_TEST_FIXTURE)
#  define DH_ACTIVE_SERVICE DH_HUB_TEST_SERVICE
#  define DH_ACTIVE_ACCOUNT DH_HUB_TEST_ACCOUNT
#else
#  define DH_ACTIVE_SERVICE DH_HUB_PROD_SERVICE
#  define DH_ACTIVE_ACCOUNT DH_HUB_PROD_ACCOUNT
#endif

/* -------------------------------------------------------------------------
 * Public ABI
 * ------------------------------------------------------------------------- */

/*
 * dh_hub_read_keychain_token
 *
 * Reads a single generic-password Keychain item (login keychain, no UI) and
 * places its raw data into *out.
 *
 * Parameters:
 *   out      - Destination buffer; must be at least `capacity` bytes.
 *   capacity - Size of the destination buffer in bytes.
 *   written  - Set to the number of bytes placed in *out on success (0..4096),
 *              or to 0 on any failure.
 *
 * Returns:
 *   0 on success, 1 on any failure (see ABI contract above).
 */
int dh_hub_read_keychain_token(void *out, size_t capacity, size_t *written)
{
    /* Initialise *written unconditionally so callers never see garbage. */
    if (written) {
        *written = 0;
    }

    /* Reject obviously invalid arguments before touching the Keychain. */
    if (!out || !written) {
        return 1;
    }

    /* -----------------------------------------------------------------------
     * Step 1: Disable user interaction.
     * We use the deprecated (but intentional) SecKeychainGetUserInteractionAllowed
     * / SecKeychainSetUserInteractionAllowed pair to ensure that the Keychain
     * never shows a UI prompt.  If we cannot read or restore the flag we abort
     * rather than risk a user-visible dialog.
     * Deprecation is suppressed narrowly around these two calls only.
     * ----------------------------------------------------------------------- */
    Boolean prev_interaction = TRUE; /* safe default: re-enable if unsure */

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    OSStatus get_status = SecKeychainGetUserInteractionAllowed(&prev_interaction);
#pragma clang diagnostic pop

    if (get_status != errSecSuccess) {
        /* Cannot determine current state; refuse to proceed. */
        return 1;
    }

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    OSStatus set_status = SecKeychainSetUserInteractionAllowed(FALSE);
#pragma clang diagnostic pop

    if (set_status != errSecSuccess) {
        /* Cannot disable interaction; refuse to read. */
        return 1;
    }

    /* -----------------------------------------------------------------------
     * Step 2: Query the Keychain item.
     * kSecMatchLimit defaults to kSecMatchLimitOne so we get at most one result.
     * kSecReturnData = kCFBooleanTrue causes the data to be returned directly.
     * ----------------------------------------------------------------------- */
    int result = 1; /* pessimistic default */
    CFDataRef data_ref = NULL;

    CFStringRef service_cf =
        CFStringCreateWithCStringNoCopy(kCFAllocatorDefault,
                                        DH_ACTIVE_SERVICE,
                                        kCFStringEncodingUTF8,
                                        kCFAllocatorNull);
    CFStringRef account_cf =
        CFStringCreateWithCStringNoCopy(kCFAllocatorDefault,
                                        DH_ACTIVE_ACCOUNT,
                                        kCFStringEncodingUTF8,
                                        kCFAllocatorNull);

    if (!service_cf || !account_cf) {
        goto restore;
    }

    {
        const void *query_keys[] = {
            kSecClass,
            kSecAttrService,
            kSecAttrAccount,
            kSecReturnData,
            kSecMatchLimit
        };
        const void *query_vals[] = {
            kSecClassGenericPassword,
            service_cf,
            account_cf,
            kCFBooleanTrue,
            kSecMatchLimitOne
        };

        CFDictionaryRef query = CFDictionaryCreate(
            kCFAllocatorDefault,
            query_keys,
            query_vals,
            (CFIndex)(sizeof(query_keys) / sizeof(query_keys[0])),
            &kCFTypeDictionaryKeyCallBacks,
            &kCFTypeDictionaryValueCallBacks);

        if (!query) {
            goto restore;
        }

        CFTypeRef raw_result = NULL;
        OSStatus copy_status = SecItemCopyMatching(query, &raw_result);
        CFRelease(query);

        if (copy_status != errSecSuccess || !raw_result) {
            /* Not found, auth failure, or any other error. */
            if (raw_result) {
                CFRelease(raw_result);
            }
            goto restore;
        }

        /* -----------------------------------------------------------------------
         * Step 3: Type-check the result.
         * SecItemCopyMatching with kSecReturnData must return CFDataRef.
         * Reject anything else defensively.
         * ----------------------------------------------------------------------- */
        if (CFGetTypeID(raw_result) != CFDataGetTypeID()) {
            CFRelease(raw_result);
            goto restore;
        }

        data_ref = (CFDataRef)raw_result;
    }

    /* -----------------------------------------------------------------------
     * Step 4: Bounds-check and copy.
     * Enforce DH_HUB_TOKEN_MAX and the caller's capacity in one expression.
     * No partial writes: if the token is larger than either limit, fail.
     * ----------------------------------------------------------------------- */
    {
        CFIndex data_len = CFDataGetLength(data_ref);

        if (data_len < 0) {
            /* Unexpected; CFData length should never be negative. */
            goto restore;
        }

        size_t token_len = (size_t)data_len;

        if (token_len > DH_HUB_TOKEN_MAX || token_len > capacity) {
            goto restore;
        }

        if (token_len > 0) {
            memcpy(out, CFDataGetBytePtr(data_ref), token_len);
        }

        *written = token_len;
        result = 0;
    }

restore:
    /* -----------------------------------------------------------------------
     * Step 5: Release resources and restore interaction flag unconditionally.
     * ----------------------------------------------------------------------- */
    if (data_ref) {
        CFRelease(data_ref);
    }
    if (service_cf) {
        CFRelease(service_cf);
    }
    if (account_cf) {
        CFRelease(account_cf);
    }

    /* Restore previous user-interaction state regardless of read outcome. */
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    (void)SecKeychainSetUserInteractionAllowed(prev_interaction);
#pragma clang diagnostic pop

    if (result != 0) {
        *written = 0;
    }

    return result;
}
