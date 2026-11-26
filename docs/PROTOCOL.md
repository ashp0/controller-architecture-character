# Brain ↔ body protocol

One WebSocket, JSON text frames. The body listens on `ws://127.0.0.1:8765/ws/brain`
and accepts one brain at a time. Viewers connect separately to `/ws/viz`.

The protocol is deliberately **semantic, not joint-level**. The brain says *where
to look* and *what to express*. The body owns kinematics, joint limits, velocity
and acceleration limits, and what to do when the brain goes away. A hardware
driver for a real lamp would implement exactly this interface.

## Brain → body

| Message | Fields | Body behaviour |
|---|---|---|
| `posture` | `name`: `sleep` \| `idle` \| `attentive` \| `loom`; `speed` 0.1–1 (fraction of URDF max velocity) | Arm (shoulder, elbow) base pose. |
| `look` | `u`, `v`: normalized camera-image coordinates (0–1, origin top-left), or `home: true` | Solves look-at IK on base yaw, neck yaw and head pitch so the light axis points at that image location. Unreachable targets saturate at the joint limits. |
| `gesture` | `name`: `nod` `shake` `recoil` `bow` `survey` `ponder` `awaken` `focus`; optional `id` | Keyframed joint offsets layered over posture and gaze. Replies `gesture_done` with the `id`. |
| `light` | `color` [r,g,b] 0–1, `intensity` 0–1, `beam` 0–1 (cone width), `pulse` Hz, `fade` s | Exponential fade to the target; pulse modulates intensity. |
| `hud` | any HUD fields | Relayed verbatim to every viewer (subtitles, mode, Ledger, camera thumbnail, latencies). |

## Body → brain

| Message | Fields |
|---|---|
| `state` (20 Hz) | `joints` {name: rad}, `light` {color, intensity, beam}, `aim` {u, v}: where the light axis currently meets the camera image, `settled` bool, `posture` |
| `gesture_done` | `id` |
| `text` | `text`: typed by a viewer (relayed) |
| `ptt` | `down` bool: push-to-talk key held in a viewer (relayed) |

`aim` is what makes closed-loop goals possible. After moving, the brain compares
where the lamp is actually pointing with where the object is in a **new** camera
frame.

## Body → viewer (`/ws/viz`)

`state` at 30 Hz (same shape as above) plus every `hud` message. A viewer sends
`text` and `ptt`.

## Failure behaviour

* Brain disconnects: the body lowers into the `sleep` posture, centres its gaze
  and fades the light out over 2 s.
* Malformed command: logged and ignored; the control loop never stops.
* Out-of-range values: clamped (light), saturated at soft limits (joints).
