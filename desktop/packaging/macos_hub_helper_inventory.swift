// macos_hub_helper_inventory.swift
// Swift 5 / macOS 12+ — standalone helper-process file, no main, no installer.
//
// Exports: dh_hub_read_inventory (C ABI, @_cdecl)
// Declares: dh_hub_read_keychain_token — external C symbol supplied by the
//           C object file; declared here with @_silgen_name (no body) so the
//           Swift compiler uses the C ABI symbol at link time.
//
//   Bridging-header alternative: if the build system provides a bridging
//   header (e.g. hub_helper_bridge.h), add:
//       int32_t dh_hub_read_keychain_token(void *out, size_t cap, size_t *written);
//   and remove the @_silgen_name declaration below; the compiler will resolve
//   the symbol through the header.  Current choice: @_silgen_name, no body.
//
// Design constraints honoured:
//   • No Security/Keychain framework — all keychain access is in the C object.
//   • No ambient mutable globals; injectable seams are function parameters.
//   • Borrowed caller buffers: never captured from async closures.
//   • Native worker thread (XPC provider is already off main); no extra queue.
//   • NSLock-protected completion result box with exactly-once result.
//   • DispatchSemaphore timeout 8 s — above the 6 s network timeout,
//     below the XPC connection timeout of 10 s.
//   • No partial output: written is set only on full success.
//   • Strict UTF-8 decoding on inventory payload.
//   • Token validated: length 1…4096, printable ASCII (0x21–0x7E header-safe).
//   • Maximum inventory payload: 2 MiB (from HubInventoryFetcher.maxBodyBytes).
//   • Injectable seams (_keychainFn / _fetchFn) are function parameters.
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

private let kDHSuccess:  Int32 = 0
private let kDHAuth:     Int32 = 1
private let kDHTempFail: Int32 = 2
private let kDHTLS:      Int32 = 3
private let kDHInvalid:  Int32 = 4

// ---------------------------------------------------------------------------
// MARK: - Semaphore timeout
// ---------------------------------------------------------------------------
// 8 s: comfortably above the 6 s network timeout in HubInventoryFetcher,
// and safely below the XPC connection-idle timeout of 10 s.

private let kSemaphoreTimeoutNs: Int64 = 8_000_000_000

// ---------------------------------------------------------------------------
// MARK: - Token limits
// ---------------------------------------------------------------------------

private let kMaxTokenBytes: UInt = 4096
private let kMaxInventoryBytes: Int = HubInventoryFetcher.maxBodyBytes  // 2 MiB

// ---------------------------------------------------------------------------
// MARK: - External C symbol: keychain token reader
// ---------------------------------------------------------------------------
// @_silgen_name with no body → external declaration only.
// The C object file (compiled separately) provides the symbol
// dh_hub_read_keychain_token with the matching C signature:
//   int32_t dh_hub_read_keychain_token(void *out, size_t capacity, size_t *written);
//
// UInt maps to size_t at the ABI level on all Apple 64-bit targets (arm64,
// x86_64), satisfying the "require size_t Swift UInt at ABI" constraint.
//
// Pointer parameters are optional (nullable) so callers that pass NULL are
// caught by nil checks before any dereference.

@_silgen_name("dh_hub_read_keychain_token")
private func dh_hub_read_keychain_token(
    _ out: UnsafeMutableRawPointer?,  // nullable — nil check before use
    _ capacity: UInt,                  // size_t at ABI: UInt
    _ written: UnsafeMutablePointer<UInt>?  // nullable — nil check before use
) -> Int32

// ---------------------------------------------------------------------------
// MARK: - Default injectable keychain closure
// ---------------------------------------------------------------------------
// Wraps the external C symbol so it can be passed as a closure value.
// This is the default injected into _readInventorySync.

private func defaultKeychainFn(
    out: UnsafeMutableRawPointer,
    capacity: UInt,
    written: UnsafeMutablePointer<UInt>
) -> Int32 {
    dh_hub_read_keychain_token(out, capacity, written)
}

// ---------------------------------------------------------------------------
// MARK: - Default injectable fetch closure
// ---------------------------------------------------------------------------

private func defaultFetchFn(
    token: String,
    completion: @escaping (HubInventoryResult) -> Void
) {
    HubInventoryFetcher.fetch(token: token, completion: completion)
}

// ---------------------------------------------------------------------------
// MARK: - Completion result box (NSLock-protected, exactly-once)
// ---------------------------------------------------------------------------
// Wraps the async fetch result so the semaphore-wait path never shares
// unprotected mutable state and never accesses caller pointers after return.

private final class ResultBox {
    private let lock = NSLock()
    private var _result: HubInventoryResult?

    /// Store the result exactly once; subsequent calls are silently ignored.
    func store(_ result: HubInventoryResult) {
        lock.lock()
        defer { lock.unlock() }
        if _result == nil { _result = result }
    }

    /// Retrieve the stored result (nil if none stored yet).
    func load() -> HubInventoryResult? {
        lock.lock()
        defer { lock.unlock() }
        return _result
    }
}

// ---------------------------------------------------------------------------
// MARK: - Synchronous inventory read (internal helper, fully testable)
// ---------------------------------------------------------------------------
// All logic lives here.  The @_cdecl entry point is a thin trampoline.
//
// Parameters
//   callerOut   – borrowed caller buffer; never passed into async closures.
//   capacity    – UInt (size_t at ABI); rejects values > kMaxInventoryBytes.
//   written     – receives byte count on success; zero on any failure.
//   keychainFn  – injectable keychain reader (default: defaultKeychainFn).
//   fetchFn     – injectable fetch driver (default: defaultFetchFn).
//
// The XPC provider already calls this on a worker thread; no extra dispatch.

internal func _readInventorySync(
    callerOut: UnsafeMutableRawPointer,
    capacity: UInt,
    written: UnsafeMutablePointer<UInt>,
    keychainFn: (
        _ out: UnsafeMutableRawPointer,
        _ capacity: UInt,
        _ written: UnsafeMutablePointer<UInt>
    ) -> Int32 = defaultKeychainFn,
    fetchFn: (
        _ token: String,
        _ completion: @escaping (HubInventoryResult) -> Void
    ) -> Void = defaultFetchFn
) -> Int32 {

    // ---- 1. Zero output length unconditionally ----
    written.pointee = 0

    // ---- 2. Validate caller capacity (defensive: reject negative-equivalent) ----
    // UInt rules out negative at the type level; still reject zero or
    // values beyond the 2 MiB inventory ceiling to enforce "no partial copy".
    guard capacity > 0, capacity <= UInt(kMaxInventoryBytes) else {
        return kDHInvalid
    }

    // ---- 3. Read keychain token into a local buffer ----
    // Stack-allocated scratch buffer; never captured by any async closure.
    var tokenBytes = [UInt8](repeating: 0, count: Int(kMaxTokenBytes))
    var tokenLen: UInt = 0

    let tokenStatus: Int32 = tokenBytes.withUnsafeMutableBytes { raw -> Int32 in
        guard let baseAddress = raw.baseAddress else { return kDHAuth }
        return keychainFn(baseAddress, kMaxTokenBytes, &tokenLen)
    }

    // Wipe scratch on any early-exit path.
    defer {
        // Void result intentionally discarded — wipe only.
        _ = tokenBytes.withUnsafeMutableBytes { (raw: UnsafeMutableRawBufferPointer) -> Void in
            raw.baseAddress?.initializeMemory(
                as: UInt8.self, repeating: 0, count: Int(kMaxTokenBytes))
        }
    }

    guard tokenStatus == kDHSuccess, tokenLen > 0 else {
        return kDHAuth
    }

    // ---- 4. Validate token: length 1…4096, printable ASCII (header-safe) ----
    let tokenCount = Int(tokenLen)
    guard (1...Int(kMaxTokenBytes)).contains(tokenCount) else {
        return kDHAuth
    }

    guard let token = String(bytes: tokenBytes.prefix(tokenCount), encoding: .utf8),
          !token.isEmpty,
          token.unicodeScalars.allSatisfy({ $0.value >= 0x21 && $0.value <= 0x7E })
    else {
        return kDHAuth
    }

    // ---- 5. Drive the async fetch synchronously via semaphore ----
    // Result is captured into an NSLock-protected box; the caller's raw
    // buffer pointer (callerOut) is never passed into the closure.
    let semaphore = DispatchSemaphore(value: 0)
    let box = ResultBox()

    fetchFn(token) { result in
        box.store(result)
        semaphore.signal()
    }

    // Wait up to 8 s.  This is a synchronous XPC provider thread; blocking is
    // expected by the XPC ABI.
    let deadline = DispatchTime.now() + .nanoseconds(Int(kSemaphoreTimeoutNs))
    guard semaphore.wait(timeout: deadline) == .success else {
        return kDHTempFail
    }

    // ---- 6. Map HubInventoryResult → C status code ----
    guard let fetchResult = box.load() else {
        // Semaphore was signalled but box is empty — treat as invalid.
        return kDHInvalid
    }

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
        // ---- 7. Validate body ----
        // Must be non-empty (HubInventoryFetcher already enforces this, but
        // double-check at ABI boundary).
        guard !body.isEmpty else { return kDHInvalid }

        // Must be valid UTF-8.
        guard String(data: body, encoding: .utf8) != nil else { return kDHInvalid }

        // Enforce 2 MiB limit plus caller capacity; reject with no partial copy.
        guard body.count <= kMaxInventoryBytes, body.count <= Int(capacity) else {
            return kDHInvalid
        }

        // ---- 8. Write to caller buffer — single, atomic copy ----
        // No partial output: written is set only here on the success path.
        // Void result discarded explicitly — no compiler warning.
        body.withUnsafeBytes { (src: UnsafeRawBufferPointer) -> Void in
            guard let srcBase = src.baseAddress else { return }
            callerOut.copyMemory(from: srcBase, byteCount: body.count)
        }
        written.pointee = UInt(body.count)
        return kDHSuccess
    }
}

// ---------------------------------------------------------------------------
// MARK: - C ABI entry point
// ---------------------------------------------------------------------------
// Signature (C):
//   int32_t dh_hub_read_inventory(void *context,
//                                  void *out,
//                                  size_t capacity,
//                                  size_t *written);
//
// context  — reserved for future XPC connection context; ignored for now.
//            Declared optional (nullable) to match C void*.
// out      — borrowed caller buffer; must remain valid until this returns.
//            Optional (nullable); nil → return kDHInvalid immediately.
// capacity — size_t mapped to UInt; zero or overflow → kDHInvalid.
// written  — receives byte count written on success; zero on failure.
//            Optional (nullable); nil → return kDHInvalid immediately.
//
// Returns one of the kDH* constants defined at the top of this file.

@_cdecl("dh_hub_read_inventory")
public func dh_hub_read_inventory(
    _ context: UnsafeMutableRawPointer?,   // nullable (reserved)
    _ out: UnsafeMutableRawPointer?,       // nullable — nil check
    _ capacity: UInt,                       // size_t at ABI: UInt
    _ written: UnsafeMutablePointer<UInt>? // nullable — nil check
) -> Int32 {
    // Nil checks before any dereference.
    guard let out = out, let written = written else {
        return kDHInvalid
    }
    // Dispatch to the testable helper with default injectable seams.
    return _readInventorySync(
        callerOut: out,
        capacity: capacity,
        written: written
    )
}
