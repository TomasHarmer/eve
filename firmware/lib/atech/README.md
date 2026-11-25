# Atech drivers

The drivers eve needs from the Atech module library, copied unchanged from the
[Atech SDK](https://pypi.org/project/atech/) 1.0.0a12 (MIT):

| File | From the SDK's `atech/catalog/data/` |
|---|---|
| `microphone.h`, `microphone.cpp` | `modules/microphone/` |
| `button.h`, `button.cpp` | `modules/button/` |
| `atech_gestures.h`, `atech_gestures.cpp`, `atech_tap_classifier.h` | `shared/` (the button's tap and hold detection) |

Don't edit them here: a fix belongs upstream, after which these files are
copied again. To add more modules to eve, see "Adding modules" in the README.
