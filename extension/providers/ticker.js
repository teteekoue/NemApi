/**
 * NemApi — timer source of the content script (dedicated Worker).
 *
 * WHY THIS FILE EXISTS
 *
 * Chrome throttles the timers of a hidden page in three tiers: on schedule
 * while visible, once per second once hidden, and — after five minutes hidden,
 * thirty seconds of silence and a timer chain of 5+ — **once per MINUTE**
 * ("intensive wake-up throttling", Chrome 88+).
 *
 * Every wait of this extension is a chained timer (a poll loop that calls
 * itself), so a background tab landed in the intensive tier: an idle check
 * that asks for 1.5 s of quiet woke up a minute later, and a whole request
 * then took 2 to 5 minutes — until the user brought the tab back to the
 * foreground, which restores the normal tier and makes everything finish at
 * once. That is the random "it only works when I look at the tab" bug.
 *
 * A dedicated Worker's timers are NOT throttled, and the `message` task that
 * wakes the page up is not a timer task either: the page-side chain restarts
 * at nesting level 1 instead of climbing to 5+. `base.js:TimerHub` therefore
 * delegates every sleep to this worker when it can be spawned, and falls back
 * to a MessageChannel bounce (same chain reset, 1 s granularity) otherwise.
 *
 * The worker owns no clock and no state beyond its pending timers: the page
 * decides what to do when a timer fires. `ping` confirms the worker is alive
 * (`base.js` uses it to report the timer mode in the proxy log).
 */
"use strict";

const timers = new Map();

self.onmessage = function (event) {
  const data = (event && event.data) || {};
  const type = data.type;
  if (type === "sleep") {
    const id = data.id;
    const ms = Math.max(0, Number(data.ms) || 0);
    const handle = setTimeout(function () {
      timers.delete(id);
      try {
        self.postMessage({ type: "wake", id: id });
      } catch (_) {}
    }, ms);
    timers.set(id, handle);
    return;
  }
  if (type === "cancel") {
    const handle = timers.get(data.id);
    if (handle !== undefined) {
      clearTimeout(handle);
      timers.delete(data.id);
    }
    return;
  }
  if (type === "ping") {
    try {
      self.postMessage({ type: "pong", id: data.id });
    } catch (_) {}
  }
};
