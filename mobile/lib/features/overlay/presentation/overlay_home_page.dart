import 'dart:async';

import 'package:flutter/material.dart';

import '../../../core/logging/app_logger.dart';
import '../../../core/platform/overlay_platform.dart';

/// Sprint 10.1 §18 — the functional permission + service setup screen.
///
/// Scope discipline: this screen only manages overlay permission and the
/// overlay runtime. No intelligence, no video detection, no final product
/// design (Sprint 10.3+).
///
/// Lifecycle (§7.4): permission is re-checked every time the app returns to
/// the foreground, so a grant granted in the system settings screen — or a
/// later revocation — is reflected without restarting the app.
class OverlayHomePage extends StatefulWidget {
  const OverlayHomePage({super.key, this.platform = overlayPlatform});

  /// Swappable for tests; production uses the MethodChannel implementation.
  final OverlayPlatform platform;

  @override
  State<OverlayHomePage> createState() => OverlayHomePageState();
}

class OverlayHomePageState extends State<OverlayHomePage>
    with WidgetsBindingObserver {
  StreamSubscription<OverlayRuntimeState>? _stateSub;
  bool? _permission;
  OverlayRuntimeState? _state;
  String? _error;
  bool _busy = false;

  OverlayPlatform get _platform => widget.platform;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _stateSub = _platform.stateChanges.listen((OverlayRuntimeState state) {
      if (!mounted) return;
      setState(() => _state = state);
    });
    unawaited(_refresh());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _stateSub?.cancel();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      // §7.4: re-check permission whenever the app returns to foreground.
      unawaited(_refresh());
    }
  }

  Future<void> _refresh() async {
    final permission = await _platform.getOverlayPermissionStatus();
    OverlayRuntimeState? state;
    try {
      state = await _platform.getOverlayState();
    } on OverlayBridgeException catch (error) {
      AppLogger.event('overlay_error', {'code': error.code ?? 'unknown'});
    }
    if (!mounted) return;
    setState(() {
      _permission = permission;
      if (state != null) _state = state;
    });
  }

  Future<void> _run(
    Future<OverlayRuntimeState> Function() action, {
    bool clearError = true,
  }) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      if (clearError) _error = null;
    });
    try {
      final state = await action();
      if (!mounted) return;
      setState(() => _state = state);
    } on OverlayBridgeException catch (error) {
      AppLogger.event('overlay_error', {'code': error.code ?? 'unknown'});
      if (!mounted) return;
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _enablePermission() async {
    setState(() => _error = null);
    await _platform.requestOverlayPermission();
    // Truth arrives on the next foreground refresh (§7.4).
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final granted = _permission ?? false;
    final state = _state;

    return Scaffold(
      appBar: AppBar(
        title: const Text('AI Overlay'),
        centerTitle: false,
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _SetupCard(
            granted: granted,
            onEnable: _busy ? null : _enablePermission,
          ),
          const SizedBox(height: 16),
          _ServiceCard(
            granted: granted,
            state: state,
            busy: _busy,
            onStart: () => _run(_platform.startOverlay),
            onStop: () => _run(_platform.stopOverlay),
            onRefresh: _refresh,
          ),
          if (_error != null) ...[
            const SizedBox(height: 16),
            _ErrorBanner(message: _error!),
          ],
          const SizedBox(height: 24),
          Text(
            'Technical foundation only — YouTube video detection and the AI '
            'overlay experience arrive in later sprints.',
            style: theme.textTheme.bodySmall?.copyWith(
              color: theme.colorScheme.outline,
            ),
            textAlign: TextAlign.center,
          ),
        ],
      ),
    );
  }
}

/// §18 setup copy: "Enable AI Overlay … [Enable Overlay]".
class _SetupCard extends StatelessWidget {
  const _SetupCard({required this.granted, required this.onEnable});

  final bool granted;
  final VoidCallback? onEnable;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Enable AI Overlay', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(
              'To show the AI intelligence layer over YouTube, allow this app '
              'to display over other apps.',
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: Text('Overlay Permission',
                      style: theme.textTheme.bodyMedium),
                ),
                Text(
                  granted ? '\u2713 Enabled' : '\u25CB Required',
                  style: theme.textTheme.bodyMedium?.copyWith(
                    color: granted ? Colors.green.shade400 : Colors.amber,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
            ),
            if (!granted) ...[
              const SizedBox(height: 16),
              SizedBox(
                width: double.infinity,
                child: FilledButton(
                  onPressed: onEnable,
                  child: const Text('Enable Overlay'),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// Runtime controls: Start / Stop with the central OverlayState visible (§15).
class _ServiceCard extends StatelessWidget {
  const _ServiceCard({
    required this.granted,
    required this.state,
    required this.busy,
    required this.onStart,
    required this.onStop,
    required this.onRefresh,
  });

  final bool granted;
  final OverlayRuntimeState? state;
  final bool busy;
  final VoidCallback onStart;
  final VoidCallback onStop;
  final VoidCallback onRefresh;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final running =
        state == OverlayRuntimeState.active || state == OverlayRuntimeState.starting;
    final stopping = state == OverlayRuntimeState.stopping;
    final transitioning =
        state == OverlayRuntimeState.starting || stopping;

    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text('Overlay Service',
                      style: theme.textTheme.titleMedium),
                ),
                IconButton(
                  tooltip: 'Refresh state',
                  onPressed: busy ? null : onRefresh,
                  icon: const Icon(Icons.refresh, size: 18),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: Text(
                    state?.wire ?? 'UNKNOWN',
                    style: theme.textTheme.bodyMedium?.copyWith(
                      fontFamily: 'monospace',
                      color: _stateColor(state),
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 16),
            if (!granted)
              Text(
                'Enable the overlay permission to start the service.',
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.outline,
                ),
              )
            else if (!running && !stopping)
              SizedBox(
                width: double.infinity,
                child: FilledButton(
                  onPressed: busy ? null : onStart,
                  child: const Text('Start AI Overlay'),
                ),
              )
            else ...[
              SizedBox(
                width: double.infinity,
                child: OutlinedButton(
                  onPressed: busy || transitioning ? null : onStop,
                  child: Text(
                    stopping
                        ? 'Stopping\u2026'
                        : state == OverlayRuntimeState.starting
                            ? 'Starting\u2026'
                            : 'Stop AI Overlay',
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Color? _stateColor(OverlayRuntimeState? state) {
    switch (state) {
      case OverlayRuntimeState.active:
        return Colors.green.shade400;
      case OverlayRuntimeState.error:
        return Colors.red.shade300;
      case OverlayRuntimeState.permissionRequired:
        return Colors.amber;
      default:
        return null;
    }
  }
}

/// §27 — friendly error surface; never a stack trace.
class _ErrorBanner extends StatelessWidget {
  const _ErrorBanner({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.red.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Colors.red.shade300.withValues(alpha: 0.5)),
      ),
      child: Row(
        children: [
          Icon(Icons.error_outline, size: 18, color: Colors.red.shade200),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              message,
              style: TextStyle(color: Colors.red.shade100, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }
}
