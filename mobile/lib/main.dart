import 'package:flutter/material.dart';

import 'features/overlay/presentation/overlay_home_page.dart';

void main() {
  runApp(const AiOverlayApp());
}

/// Root of the Android AI Overlay client (Sprint 10.1 foundation).
///
/// The app is a SEPARATE client of the existing FastAPI backend next to the
/// desktop Chrome extension — it shares backend contracts, never code.
class AiOverlayApp extends StatelessWidget {
  const AiOverlayApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'AI Overlay',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.deepPurple),
        useMaterial3: true,
      ),
      home: const OverlayHomePage(),
    );
  }
}
