# Keep the activity name stable: the desktop launches it with
# `adb shell am start -n de.kruse.stopmotion/.MainActivity`.
-keep class de.kruse.stopmotion.MainActivity { *; }
