/// Sprint 10.1 §22/§23 — the single configuration source for the Android
/// client.
///
/// The Android app is ONLY another client of the existing FastAPI backend;
/// no intelligence, models or acquisition logic ever lives here. Sprint 10.1
/// performs no network calls — [AppConfig.apiBaseUri] exists so future
/// sprints never hardcode backend URLs across the app (the desktop client
/// solves the same problem the same way: configuration, not constants).
class AppConfig {
  const AppConfig._();

  /// Backend base URL. Development default targets the host machine from an
  /// emulator (`10.0.2.2` is the emulator's alias for the host loopback) and
  /// from a real device the developer overrides it per build flavor later.
  /// No API keys or secrets are ever stored in the app (§32).
  static const String apiBaseUri = 'http://10.0.2.2:8000';

  /// Parsed form of [apiBaseUri], validated once so a malformed value fails
  /// fast at first use instead of inside a request.
  static Uri get apiBaseUriParsed => Uri.parse(apiBaseUri);
}
