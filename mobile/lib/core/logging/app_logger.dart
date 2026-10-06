import 'package:flutter/foundation.dart';

/// Sprint 10.1 §28 — structured development logging.
///
/// One narrow helper so every log line carries the same shape and the event
/// vocabulary stays centralized (mirrors the Kotlin side's `LogOverlayEvent`).
///
/// RULES
/// - Log only lifecycle/permission events from the §28 vocabulary.
/// - NEVER log user data, comment contents, YouTube data, secrets or keys.
/// - Debug builds only: [debugPrint] is stripped from release output by
///   Flutter's default `debugPrint` behavior in profile/release trees.
class AppLogger {
  const AppLogger._();

  static void event(String event, [Map<String, Object?> details = const {}]) {
    if (details.isEmpty) {
      debugPrint('[sentiment-ai] $event');
      return;
    }
    final suffix = details.entries.map((e) => '${e.key}=${e.value}').join(' ');
    debugPrint('[sentiment-ai] $event $suffix');
  }
}
