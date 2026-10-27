# Science games

Three self-contained HTML games, one for each result OpenAI claimed on October 26, 2026.
Each takes about 25 minutes, needs no installation, and runs in any modern browser.
Open the file directly, or play the published claude.ai artifact versions linked from the pull
request. Sound starts only after you press "Play with sound"; "Play muted" works too.

| Game | File | Field | What you walk away understanding |
|---|---|---|---|
| Needle & Wave | `needle-and-wave.html` | Harmonic analysis | Fourier series, the uncertainty principle, the Kakeya needle problem, fractal dimension, the Kakeya maximal conjecture, local smoothing of waves, and why one estimate at the top of a chain settles everything below it |
| The Exponential Wall | `exponential-wall.html` | Complexity theory | 3-SAT, exponential growth, DPLL search, asymptotics, reductions and NP-completeness, counting vs deciding, and what the Exponential Time Hypothesis, SETH and P vs NP do and do not say about each other |
| Gauge & Gap | `gauge-and-gap.html` | Quantum field theory | Fields and actions, gauge invariance on a lattice, Monte Carlo as a path integral, Wilson loops and confinement, asymptotic freedom, what "a theory exists" means, and the two halves of the Yang–Mills Millennium problem |

## How the games are built

- Seven levels each, in a shared shell: title screen, level map, a sticky HUD, one idea per level
  on a short card with a "Deeper" disclosure for the college-level paragraph, a quick check after
  each level, a boss level that synthesizes, and a recap that ends on "Where the frontier is":
  what OpenAI claims, who is examining it, and what a Lean check does and does not establish.
- Procedural music and sound effects via the Web Audio API. Each game has its own sound world
  (additive-synthesis pads; chiptune; a spectral drone driven by the lattice coupling). No audio
  files are shipped; everything is synthesized in the browser.
- No external libraries. Vanilla JavaScript, Canvas 2D and Google Fonts only. Progress and mute
  settings are stored in the browser's localStorage and can be reset from the map screen.
- Light and dark themes, phone-width layouts, keyboard operation, and reduced-motion support.
- Built for short attention budgets: 2 to 5 minute levels, instant feedback, streaks, a visible
  path, and a "Skip ahead" link so nothing ever traps the player.

## Accuracy

The mathematics and physics are stated at the level of a good undergraduate course and were
checked against standard references (listed inside each game under "Sources"). Where a fact or
attribution could not be confirmed, it was left out rather than guessed. The claims about OpenAI's
results are described as unreviewed claims under independent examination; nothing in these games
asserts that they are established.

## Testing

Each game was smoke-tested headless with Playwright (Chromium): load with the artifact document
skeleton, collect page errors, start the game muted and with sound, walk every level through its
exposed debug hooks (`window.__game.gotoLevel(i)`, `window.__game.completeLevel()`), and screenshot
desktop and phone widths in both themes.
