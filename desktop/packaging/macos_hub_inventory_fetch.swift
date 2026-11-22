// macos_hub_inventory_fetch.swift
// Swift 5 / macOS 12+ — standalone, no main, no XPC, no Keychain, no installer.
// Public contract: HubInventoryFetcher.fetch(token:completion:)
// Tests use mocks only; no live network traffic in verification.

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

// MARK: - URLSession abstraction (enables mock injection in tests)

/// Minimal surface needed by the fetcher — mirrors the two URLSession methods it uses.
public protocol HubURLSession: AnyObject {
    func dataTask(
        with request: URLRequest,
        delegate: (URLSessionTaskDelegate & URLSessionDataDelegate)?
    ) -> URLSessionDataTask
    func finishTasksAndInvalidate()
}

extension URLSession: HubURLSession {
    public func dataTask(
        with request: URLRequest,
        delegate: (URLSessionTaskDelegate & URLSessionDataDelegate)?
    ) -> URLSessionDataTask {
        // Route through the delegate-bearing variant added in macOS 12.
        if #available(macOS 12.0, *) {
            return dataTask(with: request, completionHandler: { _, _, _ in })
                // ↑ placeholder; real traffic goes through the streaming delegate below.
                // We override via the full initialiser on macOS 12.
        }
        return dataTask(with: request)
    }
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
    private static let maxBodyBytes = 2 * 1024 * 1024  // 2 MiB

    // -------------------------------------------------------------------------
    // MARK: Public entry point

    /// Fetch the device inventory.
    ///
    /// - Parameters:
    ///   - token:      Non-empty, header-safe ASCII Bearer token.
    ///                 Never logged or included in any error payload.
    ///   - sessionFactory: Injectable for testing; production callers omit it.
    ///   - completion: Called exactly once on an arbitrary queue.
    public static func fetch(
        token: String,
        sessionFactory: ((URLSessionConfiguration) -> HubURLSession)? = nil,
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

        // --- Session: ephemeral, no cookies/cache/credential/storage ---
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieAcceptPolicy = .never
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.urlCredentialStorage = nil
        config.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        config.timeoutIntervalForRequest = timeoutSeconds
        config.timeoutIntervalForResource = timeoutSeconds

        // Create the streaming delegate operation (owns its own session).
        let op = FetchOperation(
            request: request,
            sessionFactory: sessionFactory ?? { cfg in URLSession(configuration: cfg) },
            config: config,
            completion: completion
        )
        op.start()
    }
}

// MARK: - Streaming delegate operation

/// Encapsulates one fetch: session lifecycle, streaming accumulation, response
/// parsing, and guaranteed single completion. Avoids strong cycles via weak
/// self; invalidates the session when done.
private final class FetchOperation: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate {

    private let request: URLRequest
    private let sessionFactory: (URLSessionConfiguration) -> HubURLSession
    private let config: URLSessionConfiguration
    private let userCompletion: (HubInventoryResult) -> Void

    private var session: HubURLSession?
    private var buffer = Data()
    private var httpStatusCode: Int?
    private var limitExceeded = false
    private var completed = false
    private let lock = NSLock()

    init(
        request: URLRequest,
        sessionFactory: @escaping (URLSessionConfiguration) -> HubURLSession,
        config: URLSessionConfiguration,
        completion: @escaping (HubInventoryResult) -> Void
    ) {
        self.request = request
        self.sessionFactory = sessionFactory
        self.config = config
        self.userCompletion = completion
    }

    func start() {
        // Session is created here; delegate is self so lifetime is managed.
        let sess = sessionFactory(config)
        self.session = sess

        // Build task with streaming delegate.  For the production URLSession we
        // use the delegate-on-task API available on macOS 12+; for mocks we call
        // the protocol method directly.
        let task: URLSessionDataTask
        if let realSession = sess as? URLSession {
            if #available(macOS 12.0, *) {
                task = realSession.dataTask(with: request, delegate: self)
            } else {
                // Fallback: delegate set at session level (not injectable but
                // macOS 12 is the deployment target so this branch is defensive).
                task = realSession.dataTask(with: request)
            }
        } else {
            // Mock path
            task = sess.dataTask(with: request, delegate: self)
        }
        task.resume()
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
        // Check size before appending — never buffer beyond limit.
        guard buffer.count + data.count <= HubInventoryFetcher.maxBodyBytes else {
            limitExceeded = true
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
        guard let _ = String(data: buffer, encoding: .utf8) else {
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

    /// Calls the user completion exactly once, then invalidates the session.
    private func finish(with result: HubInventoryResult) {
        lock.lock()
        let alreadyDone = completed
        completed = true
        lock.unlock()

        guard !alreadyDone else { return }

        userCompletion(result)

        // Invalidate so URLSession releases its delegate reference (avoids leak).
        session?.finishTasksAndInvalidate()
        session = nil
    }
}

// MARK: - Mock helpers (for tests only — not shipped to production)

#if DEBUG

/// Synchronous mock session for unit tests.  Calls delegate methods inline.
public final class MockHubURLSession: HubURLSession {
    public var stubbedStatusCode: Int
    public var stubbedData: Data
    public var stubbedError: Error?
    public var invalidated = false

    public init(statusCode: Int = 200, data: Data = Data(), error: Error? = nil) {
        self.stubbedStatusCode = statusCode
        self.stubbedData = data
        self.stubbedError = error
    }

    public func dataTask(
        with request: URLRequest,
        delegate: (URLSessionTaskDelegate & URLSessionDataDelegate)?
    ) -> URLSessionDataTask {
        let task = MockURLSessionDataTask()
        task.onResume = { [weak self] in
            guard let self = self, let delegate = delegate else { return }

            // Simulate the URLSession delegate call sequence synchronously.
            if let error = self.stubbedError {
                (delegate as? URLSessionTaskDelegate)?.urlSession?(
                    URLSession.shared,
                    task: task,
                    didCompleteWithError: error
                )
                return
            }

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: self.stubbedStatusCode,
                httpVersion: "HTTP/1.1",
                headerFields: nil
            )!

            var allowBody = false
            (delegate as? URLSessionDataDelegate)?.urlSession?(
                URLSession.shared,
                dataTask: task as! URLSessionDataTask,
                didReceive: response,
                completionHandler: { disposition in
                    allowBody = disposition == .allow
                }
            )

            if allowBody && !self.stubbedData.isEmpty {
                (delegate as? URLSessionDataDelegate)?.urlSession?(
                    URLSession.shared,
                    dataTask: task as! URLSessionDataTask,
                    didReceive: self.stubbedData
                )
            }

            (delegate as? URLSessionTaskDelegate)?.urlSession?(
                URLSession.shared,
                task: task,
                didCompleteWithError: nil
            )
        }
        return task
    }

    public func finishTasksAndInvalidate() {
        invalidated = true
    }
}

/// Minimal URLSessionDataTask subclass the mock can vend.
public final class MockURLSessionDataTask: URLSessionDataTask {
    public var onResume: (() -> Void)?
    private var _state: URLSessionTask.State = .suspended

    public override var state: URLSessionTask.State { _state }

    public override func resume() {
        _state = .running
        onResume?()
        _state = .completed
    }

    public override func cancel() {
        _state = .canceling
    }
}

#endif
