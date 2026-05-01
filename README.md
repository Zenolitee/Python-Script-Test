# Fishing Assist

Screen-based helper for a cyan-bar fishing minigame. It watches a calibrated screen region, detects the cyan control bar and dark fish marker, then taps when the fish is to the right/outside of the bar and stops tapping when the fish is to the left.

## Setup

```powershell
python -m pip install -r requirements.txt
```

## Calibrate

Start the game and make the fishing UI visible, then run:

```powershell
python fish_assist.py --calibrate --preview
```

When prompted:

1. Move the mouse to the top-left corner of the fishing UI and left-click or press `F8`.
2. Move the mouse to the bottom-right corner of the fishing UI and left-click or press `F8`.
3. A preview window should show a yellow rectangle around the cyan bar and a red rectangle around the fish.

The region is saved in `fish_assist_config.json`.

## Run

GUI:

```powershell
python fish_assist.py --gui
```

Or double-click `run_fish.bat`.

The GUI shows `Cast -> Bobber -> Minigame`. The active phase turns green. Press `Start`, then use `Mouse4` to enable or pause clicking.

Modes:

- `Catch (Z)`: full loop, Cast -> Bobber -> Minigame -> Caught -> Cast
- `Bobber (X)`: only run the bobber/Reel phase
- `Fisher (C)`: only run the minigame phase

The `Calibrate` button lets you choose `Start Cast`, `Bobber`, or `Fishing`. `Start Cast` is one click point. `Bobber` and `Fishing` use top-left and bottom-right corners. `F8` also works.

Short preset:

```powershell
python fish_assist.py --fast
```

The `--fast` preset shows the preview window. If you want to run without it, omit `--fast` and do not pass `--preview`.

```powershell
python fish_assist.py --preview
```

Controls:

- `Mouse4`: toggle clicking on or paused
- `Esc`: quit

The preview shows `paused` until Mouse4 enables clicking. Clicks are sent at the current mouse position, so the pointer should no longer snap back to the calibrated center while the script is running.

## Bobber Test

The bobber helper is separate from the minigame detector. It waits until it sees the red bobber, then clicks across the lower-left `Reel` button 5 times after that red bobber disappears. The detector also checks for the grey fishing line and supports the darker `fishing_night.png` color tones.

To set the screen area used by the bobber preview:

```powershell
python fish_assist.py --calibrate-bobber
```

Move to the top-left of the bobber/Reel prompt and left-click or press `F8`, then move to the bottom-right and left-click or press `F8`.

To set the Cast/Start click point:

```powershell
python fish_assist.py --calibrate-cast
```

Hover the Cast button where it is clickable, then left-click or press `F8`.

```powershell
python fish_assist.py --fast --bobber
```

With preview enabled, this opens a separate `bobber preview` window for the saved bobber region. It marks the prompt text band with a white box, the red bobber with a red box, and the `Reel` click point with a cyan crosshair.

This is intentionally opt-in so the minigame `Reel`/cyan button visuals do not trigger the bobber logic by themselves.

When `--bobber` is enabled, only one phase runs at a time. Cast clicks the calibrated Cast point 5 times, Bobber waits for the red bobber to disappear and clicks `Reel`, then Minigame runs until green `Caught!` text is detected. After `Caught!` or the minigame ending, the script waits 5 seconds before returning to Cast.

## Tuning

If it clicks too late or too early, change the tolerance:

```powershell
python fish_assist.py --preview --deadzone 25
```

Lower values react closer to the bar edge. Higher values react earlier.
