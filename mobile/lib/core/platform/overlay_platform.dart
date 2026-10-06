import 'package:flutter/services.dart';

/// Sprint 10.1 §15/§16 — the ONE Flutter-side view of the native overlay
/// runtime.
///
/// Naming note: the brief's `OverlayState` collides with Flutter framework's
/// own `OverlayState` (widgets/overlay.dart), so the runtime state is called
/// [OverlayRuntimeState] here — same model, unambiguous import.
///
/// Native (Kotlin) remains the single source of truth for permission and
/// service state; Dart only mirrors it. Raw Android details never leak past
/// this abstraction (§16).
enum OverlayRuntimeState {
  disabled('DISABLED'),
  permissionRequired('PERMISSION_REQUIRED'),
  starting('STARTING'),
  active('ACTIVE'),
  stopping('STOPPING'),
  error('ERROR');

  const OverlayRuntimeState(this.wire);

  /// Uppercase wire value shared verbatim with the Kotlin `OverlayRuntimeState`.
  final String wire;

  /// Parses a native wire value. Returns null for unknown payloads so a
  /// future native state never crashes an older Flutter layer (§17).
  static OverlayRuntimeState? tryParse(String? raw) {
    for (final state in values) {
      if (state.wire == raw) return state;
    }
    return null;
  }
}

/// Friendly, user-facing bridge failure (§27). Never carries stack traces —
/// technical details stay in the native log.
class OverlayBridgeException implements Exception {
  const OverlayBridgeException(this.message, {this.code});

  final String message;
  final String? code;

  @override
  String toString() => 'OverlayBridgeException($code): $message';
}

/// The single abstraction the application layer talks to (§16). Implementations
/// are swappable in tests; production uses [MethodChannelOverlayPlatform].
abstract class OverlayPlatform {
  /// True when the user granted "Display over other apps".
  Future<bool> getOverlayPermissionStatus();

  /// Opens the system overlay-permission screen (no-op when already granted).
  /// Returns true when the request was handed to the system; the confirmed
  /// state arrives via a fresh [getOverlayPermissionStatus] on app resume.
  Future<bool> requestOverlayPermission();

  /// Current [OverlayRuntimeState] as reported by the native source of truth.
  Future<OverlayRuntimeState> getOverlayState();

  /// Idempotent start: an already-running runtime answers ACTIVE (§14).
  /// Throws [OverlayBridgeException] on permission denied / start failure.
  Future<OverlayRuntimeState> startOverlay();

  /// Idempotent stop: an already-stopped runtime answers DISABLED (§14).
  Future<OverlayRuntimeState> stopOverlay();

  /// Native → Flutter state events (§17). The stream currently carries the
  /// one event Sprint 10.1 needs (`overlay_state`); more event kinds can be
  /// added natively later without changing this interface.
  Stream<OverlayRuntimeState> get stateChanges;
}

class MethodChannelOverlayPlatform implements OverlayPlatform {
  const MethodChannelOverlayPlatform();

  static const MethodChannel methodChannel = MethodChannel('sentimentai/overlay');
  static const EventChannel eventChannel = EventChannel('sentimentai/overlay_events');

  /// §27 — native error codes to friendly copy. Raw messages/stack traces are
  /// never surfaced to the user.
  static String friendlyMessage(String code, String? nativeMessage) {
    switch (code) {
      case 'permission_denied':
        return 'Overlay permission is required.';
      case 'start_failed':
        return 'Unable to start AI overlay.';
      default:
        return nativeMessage ?? 'Something went wrong. Please try again.';
    }
  }

  Future<OverlayRuntimeState> _invoke(String method) async {
    try {
      // Raw invoke: native answers with a wire STRING, parsed here so a
      // future unknown state degrades to a friendly error, never a cast crash.
      final dynamic result = await methodChannel.invokeMethod(method);
      if (result is String) {
        final state = OverlayRuntimeState.tryParse(result);
        if (state != null) return state;
      }
      throw const OverlayBridgeException(
        'Something went wrong. Please try again.',
      );
    } on PlatformException catch (error) {
      throw OverlayBridgeException(
        friendlyMessage(error.code, error.message),
        code: error.code,
      );
    } on MissingPluginException {
      throw const OverlayBridgeException(
        'Unable to start AI overlay.',
        code: 'unavailable',
      );
    }
  }

  @override
  Future<bool> getOverlayPermissionStatus() async {
    try {
      return await methodChannel.invokeMethod<bool>('getOverlayPermissionStatus') ??
          false;
    } on PlatformException {
      // A failed status query can only mean "not granted" for UX purposes.
      return false;
    }
  }

  @override
  Future<bool> requestOverlayPermission() async {
    try {
      return await methodChannel.invokeMethod<bool>('requestOverlayPermission') ??
          false;
    } on PlatformException {
      return false;
    }
  }

  @override
  Future<OverlayRuntimeState> getOverlayState() =>
      _invoke('getOverlayState');

  @override
  Future<OverlayRuntimeState> startOverlay() => _invoke('startOverlay');

  @override
  Future<OverlayRuntimeState> stopOverlay() => _invoke('stopOverlay');

  @override
  Stream<OverlayRuntimeState> get stateChanges async* {
    // Stream errors (e.g. missing plugin in tests) are dropped, not fatal;
    // resume-time getOverlayState() reconciles truth anyway (§17).
    final raw = eventChannel.receiveBroadcastStream().handleError((Object _) {});
    await for (final dynamic payload in raw) {
      // Event envelope: {'event': 'overlay_state', 'state': 'ACTIVE'}.
      // Unknown envelopes/states are skipped, never fatal (§17 forward-compat).
      if (payload is! Map) continue;
      final wire = payload['state'];
      final state = OverlayRuntimeState.tryParse(wire is String ? wire : null);
      if (state != null) yield state;
    }
  }
}

/// Production instance. Tests substitute their own [OverlayPlatform].
const OverlayPlatform overlayPlatform = MethodChannelOverlayPlatform();
