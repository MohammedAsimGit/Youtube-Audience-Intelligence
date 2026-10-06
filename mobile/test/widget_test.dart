import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:ai_overlay/core/platform/overlay_platform.dart';
import 'package:ai_overlay/features/overlay/presentation/overlay_home_page.dart';

/// In-memory [OverlayPlatform] double: no channels, fully deterministic.
class FakeOverlayPlatform implements OverlayPlatform {
  FakeOverlayPlatform({
    this.permission = false,
    this.state = OverlayRuntimeState.disabled,
  });

  bool permission;
  OverlayRuntimeState state;
  OverlayBridgeException? startError;
  int requestCalls = 0;
  int startCalls = 0;
  int stopCalls = 0;

  final StreamController<OverlayRuntimeState> events =
      StreamController<OverlayRuntimeState>.broadcast();

  @override
  Future<bool> getOverlayPermissionStatus() async => permission;

  @override
  Future<bool> requestOverlayPermission() async {
    requestCalls++;
    return true;
  }

  @override
  Future<OverlayRuntimeState> getOverlayState() async => state;

  @override
  Future<OverlayRuntimeState> startOverlay() async {
    startCalls++;
    final error = startError;
    if (error != null) {
      // Mirror the native controller: a failed start lands in ERROR.
      state = OverlayRuntimeState.error;
      throw error;
    }
    state = OverlayRuntimeState.active;
    return state;
  }

  @override
  Future<OverlayRuntimeState> stopOverlay() async {
    stopCalls++;
    state = OverlayRuntimeState.disabled;
    return state;
  }

  @override
  Stream<OverlayRuntimeState> get stateChanges => events.stream;
}

/// Sprint 10.1 §18/§29 — setup screen behaviour with a fake platform.
void main() {
  Future<FakeOverlayPlatform> pump(
    WidgetTester tester,
    FakeOverlayPlatform platform,
  ) async {
    await tester.pumpWidget(
      MaterialApp(home: OverlayHomePage(platform: platform)),
    );
    await tester.pumpAndSettle();
    return platform;
  }

  testWidgets('denied permission shows Required state and Enable button',
      (tester) async {
    final platform = await pump(
      tester,
      FakeOverlayPlatform(permission: false),
    );

    expect(find.text('Enable AI Overlay'), findsOneWidget);
    expect(find.text('\u25CB Required'), findsOneWidget);
    expect(find.text('Enable Overlay'), findsOneWidget);
    // Cannot start without permission (§7.2).
    expect(find.text('Start AI Overlay'), findsNothing);

    await tester.tap(find.text('Enable Overlay'));
    await tester.pumpAndSettle();
    expect(platform.requestCalls, 1);
  });

  testWidgets('granted permission shows Enabled state and start button',
      (tester) async {
    await pump(tester, FakeOverlayPlatform(permission: true));

    expect(find.text('\u2713 Enabled'), findsOneWidget);
    expect(find.text('Enable Overlay'), findsNothing);
    expect(find.text('Start AI Overlay'), findsOneWidget);
    expect(find.text('DISABLED'), findsOneWidget);
  });

  testWidgets('start flow flips the visible state to ACTIVE', (tester) async {
    final platform = await pump(
      tester,
      FakeOverlayPlatform(permission: true),
    );

    await tester.tap(find.text('Start AI Overlay'));
    await tester.pumpAndSettle();

    expect(platform.startCalls, 1);
    expect(find.text('ACTIVE'), findsOneWidget);
    expect(find.text('Stop AI Overlay'), findsOneWidget);
    expect(find.text('Start AI Overlay'), findsNothing);
  });

  testWidgets('stop flow returns the runtime to DISABLED', (tester) async {
    final platform = await pump(
      tester,
      FakeOverlayPlatform(
        permission: true,
        state: OverlayRuntimeState.active,
      ),
    );

    expect(find.text('Stop AI Overlay'), findsOneWidget);
    await tester.tap(find.text('Stop AI Overlay'));
    await tester.pumpAndSettle();

    expect(platform.stopCalls, 1);
    expect(find.text('DISABLED'), findsOneWidget);
    expect(find.text('Start AI Overlay'), findsOneWidget);
  });

  testWidgets('native events update the visible state', (tester) async {
    final platform = await pump(
      tester,
      FakeOverlayPlatform(permission: true),
    );

    platform.events.add(OverlayRuntimeState.starting);
    await tester.pumpAndSettle();
    expect(find.text('STARTING'), findsOneWidget);

    platform.events.add(OverlayRuntimeState.active);
    await tester.pumpAndSettle();
    expect(find.text('ACTIVE'), findsOneWidget);
  });

  testWidgets('start failure surfaces the friendly message, not a crash',
      (tester) async {
    final platform = await pump(
      tester,
      FakeOverlayPlatform(permission: true),
    );
    platform.startError = const OverlayBridgeException(
      'Unable to start AI overlay.',
      code: 'start_failed',
    );

    await tester.tap(find.text('Start AI Overlay'));
    await tester.pumpAndSettle();

    // §27: friendly copy in a banner, never a stack trace.
    expect(find.text('Unable to start AI overlay.'), findsOneWidget);
    expect(find.textContaining('Exception'), findsNothing);

    // The refreshed state shows the failure (native is the source of truth).
    await tester.tap(find.byTooltip('Refresh state'));
    await tester.pumpAndSettle();
    expect(find.text('ERROR'), findsOneWidget);
  });

  testWidgets('renders without crashes (foundation smoke test)',
      (tester) async {
    await pump(tester, FakeOverlayPlatform());
    expect(find.text('AI Overlay'), findsOneWidget);
    expect(
      find.textContaining('YouTube video detection'),
      findsOneWidget,
    );
  });
}
