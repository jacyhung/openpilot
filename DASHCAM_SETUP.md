# Openpilot Dashcam Mode Setup

This document describes how to use openpilot as a standalone dashcam without CANBUS connection.

## Changes Made

The following modifications have been made to enable dashcam mode:

### 1. selfdrived.py
- Modified startup event logic to treat the mock car as recognized when in dashcam mode
- This prevents the "openpilot unavailable" warning from appearing
- When `CP.dashcamOnly=True`, the system uses `EventName.startupNoControl` instead of `EventName.startupNoCar`

### 2. mock/interface.py
- Added `ret.notCar = True` to the mock car parameters
- This enables immediate calibration without requiring car movement
- Calibration status will be set to "calibrated" on startup

### 3. card.py
- Modified CAN message waiting logic to skip waiting when `DASHCAM_MODE=1`
- Prevents the system from hanging indefinitely waiting for CAN traffic

### 4. controlsd.py
- Modified lane visibility to always show lane lines when in passive mode
- Changed `hudControl.lanesVisible = CC.enabled` to `hudControl.lanesVisible = CC.enabled or self.CP.passive`

### 5. events.py
- Suppressed "Dashcam Mode" alert text by using `EmptyAlert` for `startupNoControl` and `dashcamMode` events
- UI shows clean camera view without status banners

### 6. mici/onroad/model_renderer.py
- Removed the `DISENGAGED` gate that hid lane lines and path when not engaged
- Lane lines and path now always render regardless of engagement state

### 7. system/loggerd/uploader.py
- Added camera file priorities (`ecamera.hevc`, `fcamera.hevc`, `dcamera.hevc`, `rlog`) to upload queue
- Modified `next_file_to_upload` to return ALL file types, not just qlogs/qcamera
- Upload priority: boot/crash > qlog/qcamera > rlog > ecamera > fcamera > dcamera > remaining
- Reduced idle backoff from 60s (offroad) to 5s so uploads happen promptly while onroad
- Uploads now happen on cellular/LTE (no metered filtering) and while onroad

## How to Use

### Step 1: Set Environment Variable

Add the following to your `launch_env.sh` or set it before starting openpilot:

```bash
export DASHCAM_MODE=1
export STARTED=1
```

### Step 2: Start Openpilot

Start openpilot normally. The system will:
1. Skip waiting for CAN messages
2. Use the mock car interface
3. Immediately show calibration as valid
4. Display lane lines and road edges from the camera
5. Show "Dashcam Mode" status instead of "openpilot unavailable"

### Step 3: UI Behavior

The UI will display:
- Camera feed from road camera
- Lane line detection (left and right lanes)
- Road edges
- "Dashcam Mode" status indicator
- No engagement capability (as expected for dashcam mode)

## What Works

- Camera feed display
- Lane line detection and visualization
- Road edge detection
- Calibration (immediate, no movement required)
- Full UI without "unavailable" warnings

## What Doesn't Work

- Car control/engagement (intentionally disabled)
- CAN-based features (speed, steering angle, etc.)
- Any features requiring car connection

## Technical Details

### Mock Car Parameters
- Brand: "mock"
- Mass: 1700 kg
- Wheelbase: 2.70 m
- Steer Ratio: 13
- `dashcamOnly`: True
- `notCar`: True

### Calibration Behavior
With `notCar=True`, the calibrator immediately sets:
- `calStatus`: calibrated
- `calPerc`: 100%
- `rpyCalib`: [0, 0, 0]
- `validBlocks`: INPUTS_NEEDED

This means calibration is valid on startup without requiring any car movement.

### Passive Mode
When `CP.passive=True`:
- State machine doesn't update enabled/active states
- System stays in read-only mode
- No control outputs are sent
- Lane lines are still displayed (due to our modification)

## Troubleshooting

### Issue: System still shows "openpilot unavailable"
- Verify `DASHCAM_MODE=1` is set
- Check that you're using the modified code

### Issue: Lane lines not showing
- Verify calibration is valid (should be immediate with `notCar=True`)
- Check that `modelV2` messages are being received
- Verify `liveCalibration` messages are being received

### Issue: System hangs on startup
- Verify `DASHCAM_MODE=1` is set before starting
- Check that the `card.py` modification is in place

## Notes

- This is intended for dashcam use only - no control capability
- The device should not be connected to any CANBUS
- All car-related data will be from the mock car interface
- The system will not attempt to control the vehicle in any way
