// macos_hub_helper_inventory.swift
// Swift 5 / macOS 12+ — standalone helper-process file, no main, no installer.
//
// Exports: dh_hub_read_inventory (C ABI, @_cdecl)
// Declares: dh_hub_read_keychain_token (@_silgen_name, private to this process)
//
// Design constraints honoured:
//   • No token logs, no ambient config, no UI.
//   • Borrowed caller buffers: never captured from async closures.
//   • XPC calls only on the dedicated worker queue.
//   • DispatchSemaphore timeout 8 s — above the 6 s network timeout,
//     below the XPC connection timeout of 10 s.
//   • No partial output: written is set only on full success.
//   • Strict UTF-8 decoding on both keychain token and inventory payload.
//   • Maximum keychain token output: 4096 bytes.
//   • Maximum inventory payload: 2 MiB (from HubInventoryFetcher.maxBodyBytes).
//   • Injectable seams (_readKeychainToken / _performFetch) for mock testing.
//   • Does not modify HubInventoryFetcher or any other supplied file.

import Foundation

// ---------------------------------------------------------------------------
// MARK: - Return-code constants (Int32)
// ---------------------------------------------------------------------------
// 0  success
// 1  missing or invalid token (auth)
// 2  temporary / network failure
// 3  TLS / certificate failure
// 4  invalid / unexpected response

private let kDHSuccess:   Int32 = 0
private let kDHAuth:      Int32 = 1
private let kDHTempFail:  Int32 = 2
private let kDHTLS:       Int32 = 3
private let kDHInvalid:   Int32 = 4

// ---------------------------------------------------------------------------
// MARK: - Worker queue
// ---------------------------------------------------------------------------
// All outbound XPC calls are dispatched on this serial queue so they never
// execute on arbitrary threads (required by the XPC provider ABI contract).

private let _workerQueue = DispatchQueue(
    label: "com.molokohub.hub-helper-inventory.worker",
    qos: .userInitiated
)

// ---------------------------------------------------------------------------
// MARK: - Semaphore timeout
// ---------------------------------------------------------------------------
// 8 s: comfortably above the 6 s network timeout in HubInventoryFetcher,
// and safely below the XPC connection-idle timeout of 10 s.

private let _semaphoreTimeoutNs: Int64 = 8_000_000_000  // 8 s in nanoseconds

// ---------------------------------------------------------------------------
// MARK: - Maximum keychain token size
// ---------------------------------------------------------------------------

private let kMaxKeychainTokenBytes = 4096

// ---------------------------------------------------------------------------
// MARK: - Keychain token loader (private to this process)
// ---------------------------------------------------------------------------
// @_silgen_name causes the Swift compiler to emit the symbol with the given
// C name without mangling.  The symbol has internal linkage (private) and is
// not visible outside this translation unit.
//
// Signature: int32_t dh_hub_read_keychain_token(void *out, size_t capacity, size_t *written)
//
// Returns:
//   0  — token written to *out, byte count in *written
//   1  — keychain item absent or value is not valid UTF-8 / is empty
//   4  — output buffer too small to hold the token

@_silgen_name("dh_hub_read_keychain_token")
private func _keychainTokenEntry(
    _ out: UnsafeMutableRawPointer,
    _ capacity: Int,          // Swift Int == size_t on arm64
    _ written: UnsafeMutablePointer<Int>
) -> Int32 {
    // Forward to the injectable seam so tests can substitute a mock.
    return _readKeychainToken(out: out, capacity: capacity, written: written)
}

// ---------------------------------------------------------------------------
// MARK: - Injectable seam: keychain read
// ---------------------------------------------------------------------------
// Tests replace this closure with a mock that writes a synthetic token into
// the caller-supplied buffer without touching the real Keychain.
// Never capture the caller's raw output pointers in a stored closure.

internal var _readKeychainToken: (
    _ out: UnsafeMutableRawPointer,
    _ capacity: Int,
    _ written: UnsafeMutablePointer<Int>
) -> Int32 = _keychainTokenImpl

private func _keychainTokenImpl(
    out: UnsafeMutableRawPointer,
    capacity: Int,
    written: UnsafeMutablePointer<Int>
) -> Int32 {
    // Service name and account key that the helper's entitlement grants access to.
    let service = "com.molokohub.hub-helper"
    let account = "api-token"

    let query: [CFString: Any] = [
        kSecClass:            kSecClassGenericPassword,
        kSecAttrService:      service as CFString,
        kSecAttrAccount:      account as CFString,
        kSecReturnData:       kCFBooleanTrue!,
        kSecMatchLimit:       kSecMatchLimitOne,
    ]

    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)

    guard status == errSecSuccess, let data = result as? Data, !data.isEmpty else {
        // Item absent or inaccessible → auth error.
        return kDHAuth
    }

    // Enforce 4096-byte cap before anything else.
    guard data.count <= kMaxKeychainTokenBytes else {
        return kDHInvalid
    }

    // Strict UTF-8 validation — the token must round-trip through String.
    guard String(data: data, encoding: .utf8) != nil else {
        return kDHAuth
    }

    // Buffer check.
    guard data.count <= capacity else {
        return kDHInvalid
    }

    // Copy bytes into caller's buffer (borrowed, not captured).
    data.withUnsafeBytes { src in
        out.copyMemory(from: src.baseAddress!, byteCount: data.count)
    }
    written.pointee = data.count
    return kDHSuccess
}

// ---------------------------------------------------------------------------
// MARK: - Injectable seam: async fetch
// ---------------------------------------------------------------------------
// Tests replace this closure with a synchronous mock completion without
// touching the real network.  Never capture the caller's raw output pointers
// from within the async completion block.

internal var _performFetch: (
    _ token: String,
    _ completion: @escaping (HubInventoryResult) -> Void
) -> Void = { token, completion in
    HubInventoryFetcher.fetch(token: token, completion: completion)
}

// ---------------------------------------------------------------------------
// MARK: - Synchronous inventory read (internal helper, fully testable)
// ---------------------------------------------------------------------------
// All logic lives here.  The @_cdecl entry point is a thin trampoline.
// outBuffer and capacityBytes are Swift-local copies; the raw caller pointer
// is used only for a final, one-shot copy after all validation succeeds.

internal func _readInventorySync(
    callerOut: UnsafeMutableRawPointer,
    capacity: Int,
    written: UnsafeMutablePointer<Int>
) -> Int32 {

    // ---- 1. Zero output length unconditionally ----
    written.pointee = 0

    // ---- 2. Read keychain token into a local buffer ----
    // We use a local Swift array so the token is never stored in heap objects
    // accessible from async closures, and never reaches any log.
    var tokenBytes = [UInt8](repeating: 0, count: kMaxKeychainTokenBytes)
    var tokenLen = 0
    let tokenStatus: Int32 = tokenBytes.withUnsafeMutableBytes { raw in
        _readKeychainToken(
            out: raw.baseAddress!,
            capacity: kMaxKeychainTokenBytes,
            written: &tokenLen
        )
    }

    guard tokenStatus == kDHSuccess, tokenLen > 0 else {
        // Wipe the scratch buffer even if the read failed.
        tokenBytes.withUnsafeMutableBytes { $0.baseAddress!.initializeMemory(as: UInt8.self, repeating: 0, count: kMaxKeychainTokenBytes) }
        return kDHAuth
    }

    // Decode token to String for the fetcher.  Must be valid UTF-8.
    guard let token = String(bytes: tokenBytes.prefix(tokenLen), encoding: .utf8),
          !token.isEmpty
    else {
        tokenBytes.withUnsafeMutableBytes { $0.baseAddress!.initializeMemory(as: UInt8.self, repeating: 0, count: kMaxKeychainTokenBytes) }
        return kDHAuth
    }

    // Wipe stack copy now that we have the Swift String.
    tokenBytes.withUnsafeMutableBytes { $0.baseAddress!.initializeMemory(as: UInt8.self, repeating: 0, count: kMaxKeychainTokenBytes) }

    // ---- 3. Drive the async fetch synchronously via semaphore ----
    // Result is captured by value into a local Swift enum; the caller's raw
    // buffer pointer (callerOut) is never passed into the closure.
    let semaphore = DispatchSemaphore(value: 0)
    var fetchResult: HubInventoryResult = .invalidResponse

    // All XPC/network dispatch happens on the worker queue.
    _workerQueue.async {
        _performFetch(token) { result in
            // Store into an owned local — no raw pointer involved.
            fetchResult = result
            semaphore.signal()
        }
    }

    // Wait up to 8 s.  This is a synchronous XPC provider thread; blocking is
    // expected by the XPC ABI.
    let waitDeadline = DispatchTime.now() + .nanoseconds(Int(_semaphoreTimeoutNs))
    guard semaphore.wait(timeout: waitDeadline) == .success else {
        // Timeout: treat as temporary failure.
        return kDHTempFail
    }

    // ---- 4. Map HubInventoryResult → C status code ----
    switch fetchResult {
    case .needsAuthorization:
        return kDHAuth
    case .temporaryFailure:
        return kDHTempFail
    case .rejected:
        return kDHTLS
    case .invalidResponse:
        return kDHInvalid
    case .success(let body):
        // ---- 5. Validate body ----
        // Must be non-empty (already enforced by HubInventoryFetcher, but
        // double-check at ABI boundary).
        guard !body.isEmpty else { return kDHInvalid }

        // Must be valid UTF-8.
        guard String(data: body, encoding: .utf8) != nil else { return kDHInvalid }

        // Must fit in the caller's buffer.
        guard body.count <= capacity else { return kDHInvalid }

        // ---- 6. Write to caller buffer — single, atomic copy ----
        // No partial output: written is set only here on the success path.
        body.withUnsafeBytes { src in
            callerOut.copyMemory(from: src.baseAddress!, byteCount: body.count)
        }
        written.pointee = body.count
        return kDHSuccess
    }
}

// ---------------------------------------------------------------------------
// MARK: - C ABI entry point
// ---------------------------------------------------------------------------
// Signature: int32_t dh_hub_read_inventory(void *context, void *out, size_t capacity, size_t *written)
//
// context — reserved for future XPC connection context; ignored for now.
// out     — borrowed caller buffer; must remain valid until this function returns.
// capacity — byte length of *out.
// written  — receives byte count written on success; zero on failure.
//
// Returns one of the kDH* constants defined at the top of this file.

@_cdecl("dh_hub_read_inventory")
public func dh_hub_read_inventory(
    _ context: UnsafeMutableRawPointer?,
    _ out: UnsafeMutableRawPointer,
    _ capacity: Int,          // Swift Int == size_t on arm64
    _ written: UnsafeMutablePointer<Int>
) -> Int32 {
    return _readInventorySync(callerOut: out, capacity: capacity, written: written)
}
