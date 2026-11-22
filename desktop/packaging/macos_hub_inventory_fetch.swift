// macos_hub_inventory_fetch.swift
// Swift 5 / macOS 12+ — standalone, no main, no XPC, no Keychain, no installer.
// Public contract: HubInventoryFetcher.fetch(token:completion:)
// Tests use an in-process URLProtocol mock; no live network traffic in verification.

import Foundation

// MARK: - Public result type

public enum HubInventoryResult {
    /// HTTP 200 with a valid, non-empty UTF-8 JSON payload that contains a
    /// "devices" array (or is itself a JSON array).
    case success(Data)
    /// HTTP 401 or 403 — caller must re-authorise.
    case needsAuthorization
    /// HTTP 5xx or transient network error — may retry later.
    case temporaryFailure
    /// TLS / certificate trust failure — do not retry without investigation.
    case rejected
    /// Any other unexpected response (bad JSON, wrong content shape, …).
    case invalidResponse
}

// MARK: - Fetcher

/// Standalone Hub inventory fetcher.
/// One call → one isolated URLSession → one completion (guaranteed exactly once).
/// No cookies, no cache, no credential storage, no redirects.
public final class HubInventoryFetcher {

    // The only endpoint this fetcher will ever contact.
    private static let endpointURL = URL(string: "https://droidhub.molokohub.site/api/devices")!

    // Hard limits
    private static let timeoutSeconds: TimeInterval = 6
    // Internal visibility so DEBUG tests can reference the limit.
    static let maxBodyBytes = 2 * 1024 * 1024  // 2 MiB

    // -------------------------------------------------------------------------
    // MARK: Public entry point

    /// Fetch the device inventory.
    ///
    /// - Parameters:
    ///   - token:      Non-empty, header-safe ASCII Bearer token.
    ///                 Never logged or included in any error payload.
    ///   - completion: Called exactly once on an arbitrary queue.
    public static func fetch(
        token: String,
        completion: @escaping (HubInventoryResult) -> Void
    ) {
        _fetch(token: token, protocolClasses: nil, completion: completion)
    }

#if DEBUG
    /// Testing-only entry point. Supplies URLProtocol subclasses into an
    /// otherwise fixed ephemeral configuration so tests can intercept traffic
    /// entirely in-process without any live network. Absent in production builds.
    internal static func fetch(
        token: String,
        protocolClasses: [URLProtocol.Type],
        completion: @escaping (HubInventoryResult) -> Void
    ) {
        _fetch(token: token, protocolClasses: protocolClasses, completion: completion)
    }
#endif

    // -------------------------------------------------------------------------
    // MARK: Shared implementation

    private static func _fetch(
        token: String,
        protocolClasses: [AnyClass]?,
        completion: @escaping (HubInventoryResult) -> Void
    ) {
        // --- Validate token (non-empty, printable ASCII, no control chars) ---
        guard !token.isEmpty,
              token.unicodeScalars.allSatisfy({ $0.value >= 0x21 && $0.value <= 0x7E })
        else {
            completion(.invalidResponse)
            return
        }

        // --- Build request ---
        var request = URLRequest(
            url: endpointURL,
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
            timeoutInterval: timeoutSeconds
        )
        request.httpMethod = "GET"
        // Bearer token only to this exact endpoint (enforced by hardcoded URL above).
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        // --- Session configuration: ephemeral, no cookies/cache/credential storage ---
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieAcceptPolicy = .never
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.urlCredentialStorage = nil
        config.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        config.timeoutIntervalForRequest = timeoutSeconds
        config.timeoutIntervalForResource = timeoutSeconds
        if let classes = protocolClasses {
            config.protocolClasses = classes
        }

        // FetchOperation is the delegate; URLSession retains it.
        // start() creates the session and fires the task.
        let op = FetchOperation(request: request, config: config, completion: completion)
        op.start()
    }
}

// MARK: - Streaming delegate operation

/// Encapsulates one fetch: session lifecycle, streaming accumulation, response
/// parsing, and guaranteed single completion.
///
/// URLSession retains its delegate for the full session lifetime.  After
/// `finishTasksAndInvalidate()` the session releases its delegate reference,
/// breaking the retain cycle and allowing the graph to be collected.
///
/// All mutable state below is touched exclusively on the serial `delegateQueue`,
/// so no secondary synchronisation is needed for them.  `completed` / `lock`
/// guard the single-fire invariant across the two possible concurrent entry
/// points (`didReceive response` cancel path and `didCompleteWithError`).
///
/// @unchecked Sendable: safe because all mutations are confined to the serial
/// delegateQueue established at session creation time.
private final class FetchOperation: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate,
                                    @unchecked Sendable {

    private let request: URLRequest
    private let config: URLSessionConfiguration
    private let userCompletion: (HubInventoryResult) -> Void

    // Set once in start(), never mutated again.
    private var session: URLSession?

    // Mutated only on the serial delegateQueue.
    private var buffer = Data()
    private var httpStatusCode: Int?

    // Guards the single-fire invariant; two delegate methods may concurrently
    // attempt to call finish(with:) before a cancel propagates.
    private var completed = false
    private let lock = NSLock()

    init(
        request: URLRequest,
        config: URLSessionConfiguration,
        completion: @escaping (HubInventoryResult) -> Void
    ) {
        self.request = request
        self.config = config
        self.userCompletion = completion
    }

    func start() {
        // Explicitly serial queue: all URLSession delegate callbacks arrive on
        // exactly one thread, keeping delegate-state mutations race-free.
        let q = OperationQueue()
        q.maxConcurrentOperationCount = 1
        q.name = "com.molokohub.hub-inventory-fetcher"

        // URLSession(configuration:delegate:delegateQueue:) — the session retains
        // `self` as delegate until invalidated.
        let sess = URLSession(configuration: config, delegate: self, delegateQueue: q)
        session = sess

        // Plain dataTask — delegate callbacks drive all logic; no completion block.
        sess.dataTask(with: request).resume()
    }

    // -------------------------------------------------------------------------
    // MARK: URLSessionTaskDelegate — redirect refusal

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        // Refuse all redirects (including same-origin) per spec.
        completionHandler(nil)
    }

    // -------------------------------------------------------------------------
    // MARK: URLSessionDataDelegate — streaming accumulation

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        guard let http = response as? HTTPURLResponse else {
            completionHandler(.cancel)
            finish(with: .invalidResponse)
            return
        }
        httpStatusCode = http.statusCode
        // If status indicates we won't use the body, cancel early.
        switch http.statusCode {
        case 200:
            completionHandler(.allow)
        case 401, 403:
            completionHandler(.cancel)
            finish(with: .needsAuthorization)
        case 500...599:
            completionHandler(.cancel)
            finish(with: .temporaryFailure)
        default:
            completionHandler(.cancel)
            finish(with: .invalidResponse)
        }
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive data: Data
    ) {
        // Enforce size limit before appending — never buffer beyond the ceiling.
        guard buffer.count + data.count <= HubInventoryFetcher.maxBodyBytes else {
            dataTask.cancel()
            finish(with: .invalidResponse)
            return
        }
        buffer.append(data)
    }

    // -------------------------------------------------------------------------
    // MARK: URLSessionTaskDelegate — completion

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        if let err = error {
            // Trust / certificate errors → .rejected; others → .temporaryFailure.
            let nsErr = err as NSError
            if nsErr.domain == NSURLErrorDomain {
                switch nsErr.code {
                case NSURLErrorServerCertificateUntrusted,
                     NSURLErrorServerCertificateHasBadDate,
                     NSURLErrorServerCertificateHasUnknownRoot,
                     NSURLErrorServerCertificateNotYetValid,
                     NSURLErrorClientCertificateRejected,
                     NSURLErrorSecureConnectionFailed:
                    finish(with: .rejected)
                default:
                    finish(with: .temporaryFailure)
                }
            } else {
                finish(with: .temporaryFailure)
            }
            return
        }

        // No error — validate body only for 200 responses.
        guard httpStatusCode == 200 else {
            // Status-based finish already called in didReceive response.
            finish(with: .invalidResponse)
            return
        }

        // Must be non-empty.
        guard !buffer.isEmpty else {
            finish(with: .invalidResponse)
            return
        }

        // Must be valid UTF-8.
        guard String(data: buffer, encoding: .utf8) != nil else {
            finish(with: .invalidResponse)
            return
        }

        // Must be a JSON object with a "devices" key, or a JSON array.
        guard let json = try? JSONSerialization.jsonObject(with: buffer, options: []) else {
            finish(with: .invalidResponse)
            return
        }

        let hasDevices: Bool
        if let obj = json as? [String: Any] {
            hasDevices = obj["devices"] != nil
        } else if json is [[String: Any]] || json is [Any] {
            hasDevices = true
        } else {
            hasDevices = false
        }

        guard hasDevices else {
            finish(with: .invalidResponse)
            return
        }

        finish(with: .success(buffer))
    }

    // -------------------------------------------------------------------------
    // MARK: Trust evaluation

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        // Default system TLS evaluation; no override or bypass.
        completionHandler(.performDefaultHandling, nil)
    }

    // -------------------------------------------------------------------------
    // MARK: Helpers

    /// Calls the user completion exactly once, then invalidates the session so
    /// URLSession releases its delegate reference (avoids a retain cycle).
    private func finish(with result: HubInventoryResult) {
        lock.lock()
        let alreadyDone = completed
        completed = true
        lock.unlock()

        guard !alreadyDone else { return }

        userCompletion(result)

        // finishTasksAndInvalidate lets any in-flight tasks drain before the
        // session releases its strong reference to self (the delegate).
        session?.finishTasksAndInvalidate()
        session = nil
    }
}
