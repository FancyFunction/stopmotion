set shell := ["bash", "-uc"]

venv := "desktop/.venv"
py   := venv + "/bin/python"

# Create the Python venv and install the desktop app
# NOTE: this box has no ensurepip/python3-venv and no system pip, so the venv
# is created without pip and pip is bootstrapped from bootstrap.pypa.io.
# If `apt install python3.12-venv` is ever run, the --without-pip dance and the
# get-pip.py download can both be dropped. See DECISIONS.md #1.
setup:
    python3 -m venv --without-pip {{venv}}
    curl -sS -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
    {{py}} /tmp/get-pip.py -q
    {{py}} -m pip install -q PySide6 pytest

# Run the desktop application
desktop:
    {{py}} -m stopmotion

# Run the desktop app against the fake phone (no hardware needed)
fake:
    {{py}} tools/fake_phone.py &
    sleep 1
    STOPMOTION_FAKE=1 {{py}} -m stopmotion

# Python tests
test:
    {{py}} -m pytest desktop/tests -q

# Build the Android app
android-build:
    cd android && ./gradlew assembleDebug

# Install the Android app on the connected device
android-install:
    cd android && ./gradlew installDebug

# Install the phone app, launch it, then start the desktop app
run: android-install
    adb shell am start -n de.kruse.stopmotion/.MainActivity
    sleep 2
    {{py}} -m stopmotion
