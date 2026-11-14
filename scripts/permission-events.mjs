import { appendFileSync } from "node:fs";

// Deliberately excludes tool inputs, outputs, replies and credentials.
export const PermissionEvents = async () => ({
  event: async ({ event }) => {
    if (!["permission.asked", "permission.replied"].includes(event.type)) return;
    const p = event.properties || {};
    const text = (s) => typeof s === "string" ? s.slice(0, 2048) : "";
    const record = {
      timestamp: new Date().toISOString(),
      launch_id: process.env.ROBOMATE_PERMISSION_LAUNCH,
      type: event.type,
      session: text(p.sessionID),
      request: text(p.id || p.requestID),
      permission: text(p.permission),
      patterns: Array.isArray(p.patterns) ? p.patterns.slice(0, 8).map(text) : [],
    };
    try {
      appendFileSync(process.env.ROBOMATE_PERMISSION_EVENTS,
        JSON.stringify(record) + "\n", { mode: 0o600 });
    } catch {
      console.error("robomate: permission trace write failed");
    }
  },
});
