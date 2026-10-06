import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:ai_overlay/core/platform/overlay_platform.dart';

/// Sprint 10.1 §29 — MethodChannel/EventChannel contract tests for the single
/// Dart↔native overlay abstraction (wire parsing + §27 friendly errors).
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  const methodChannel = MethodChannel('sentimentai/overlay');
  const eventChannel = EventChannel('sentimentai/overlay_events');
  const platform = MethodChannelOverlayPlatform();

  final received = <String>[];

  void setHandler(Future<Object?> Function(MethodCall call)? handler) {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(methodChannel, handler);
  }

  setUp(() {
    received.clear();
    setHandler((call) async {
      received.add(call.method);
      switch (call.method) {
        case 'getOverlayPermissionStatus':
        case 'requestOverlayPermission':
          return true;
        case 'getOverlayState':
        case 'startOverlay':
        case 'stopOverlay':
          return 'DISABLED';
        default:
          return null;
      }
    });
  });

  tearDown(() => setHandler(null));

  group('method channel', () {
    test('getOverlayPermissionStatus returns the native bool', () async {
      expect(await platform.getOverlayPermissionStatus(), isTrue);
      setHandler((_) async => false);
      expect(await platform.getOverlayPermissionStatus(), isFalse);
    });

    test('permission status failure degrades to false, never throws', () async {
      setHandler((_) async {
        throw PlatformException(code: 'unavailable');
      });
      expect(await platform.getOverlayPermissionStatus(), isFalse);
      expect(await platform.requestOverlayPermission(), isFalse);
    });

    test('requestOverlayPermission is forwarded to native', () async {
      expect(await platform.requestOverlayPermission(), isTrue);
      expect(received, ['requestOverlayPermission']);
    });

    test('state methods parse the wire string', () async {
      expect(await platform.getOverlayState(), OverlayRuntimeState.disabled);
      expect(await platform.startOverlay(), OverlayRuntimeState.disabled);
      expect(await platform.stopOverlay(), OverlayRuntimeState.disabled);
      expect(
        received,
        ['getOverlayState', 'startOverlay', 'stopOverlay'],
      );

      setHandler((_) async => 'ACTIVE');
      expect(await platform.getOverlayState(), OverlayRuntimeState.active);
      expect(await platform.startOverlay(), OverlayRuntimeState.active);
    });

    test('unknown wire value becomes a friendly error, never a cast crash',
        () async {
      setHandler((_) async => 'SOME_FUTURE_STATE');
      await expectLater(
        platform.getOverlayState(),
        throwsA(
          isA<OverlayBridgeException>().having(
            (e) => e.message,
            'message',
            'Something went wrong. Please try again.',
          ),
        ),
      );
    });

    test('permission_denied maps to §27 copy', () async {
      setHandler((_) async {
        throw PlatformException(
          code: 'permission_denied',
          message: 'raw native detail',
        );
      });
      await expectLater(
        platform.startOverlay(),
        throwsA(
          isA<OverlayBridgeException>()
              .having((e) => e.code, 'code', 'permission_denied')
              .having(
                (e) => e.message,
                'message',
                'Overlay permission is required.',
              ),
        ),
      );
    });

    test('start_failed maps to §27 copy', () async {
      setHandler((_) async {
        throw PlatformException(
          code: 'start_failed',
          message: 'raw native detail',
        );
      });
      await expectLater(
        platform.startOverlay(),
        throwsA(
          isA<OverlayBridgeException>()
              .having((e) => e.code, 'code', 'start_failed')
              .having(
                (e) => e.message,
                'message',
                'Unable to start AI overlay.',
              ),
        ),
      );
    });

    test('missing native plugin degrades to unavailable, not a crash',
        () async {
      setHandler(null); // no handler -> MissingPluginException
      await expectLater(
        platform.startOverlay(),
        throwsA(
          isA<OverlayBridgeException>()
              .having((e) => e.code, 'code', 'unavailable')
              .having(
                (e) => e.message,
                'message',
                'Unable to start AI overlay.',
              ),
        ),
      );
    });
  });

  group('event stream', () {
    test('yields known overlay_state events and skips everything else',
        () async {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockStreamHandler(
        eventChannel,
        MockStreamHandler.inline(
          onListen: (args, events) {
            events.success({'event': 'overlay_state', 'state': 'STARTING'});
            // Unknown future state: skipped, never fatal (§17).
            events.success({'event': 'overlay_state', 'state': 'QUANTUM'});
            // Wrong envelope: skipped.
            events.success({'event': 'something_else'});
            // Non-map payload: skipped.
            events.success('nonsense');
            events.success({'event': 'overlay_state', 'state': 'ACTIVE'});
            events.endOfStream();
          },
        ),
      );

      expect(
        await platform.stateChanges.toList(),
        [OverlayRuntimeState.starting, OverlayRuntimeState.active],
      );
    });
  });
}
