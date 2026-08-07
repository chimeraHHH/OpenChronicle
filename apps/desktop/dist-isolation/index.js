(() => {
  "use strict";

  const MAX_REQUEST_BYTES = 65_536;
  const MAX_DEPTH = 16;
  const MAX_NODES = 4_096;
  const FORBIDDEN_KEYS = new Set(["__proto__", "constructor", "prototype"]);
  const ALLOWED_COMMANDS = new Set([
    "get_snapshot",
    "get_candidate",
    "edit_candidate",
    "approve_candidate",
    "reject_candidate",
    "preview_forget_candidate",
    "forget_candidate",
    "get_daily_wrap",
    "trace_provenance",
    "resolve_evidence",
    "set_capture_paused",
  ]);
  const EVENT_LISTEN_COMMAND = "plugin:event|listen";
  const EVENT_UNLISTEN_COMMAND = "plugin:event|unlisten";
  const NAVIGATION_EVENT = "desktop:navigate";

  const isPlainRecord = (value) => {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return false;
    }
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  };

  const inspectValue = (root) => {
    const pending = [{ value: root, depth: 0 }];
    let nodes = 0;

    while (pending.length > 0) {
      const { value, depth } = pending.pop();
      nodes += 1;
      if (nodes > MAX_NODES || depth > MAX_DEPTH) {
        throw new Error("OpenChronicle request is too complex");
      }

      if (value === null) continue;
      const valueType = typeof value;
      if (valueType === "string" || valueType === "boolean") continue;
      if (valueType === "number") {
        if (!Number.isFinite(value)) {
          throw new Error("OpenChronicle request contains a non-finite number");
        }
        continue;
      }
      if (valueType !== "object") {
        throw new Error("OpenChronicle request contains an unsupported value");
      }

      if (!Array.isArray(value) && !isPlainRecord(value)) {
        throw new Error("OpenChronicle request contains a non-plain object");
      }

      for (const key of Object.keys(value)) {
        if (FORBIDDEN_KEYS.has(key)) {
          throw new Error("OpenChronicle request contains a forbidden key");
        }
        pending.push({ value: value[key], depth: depth + 1 });
      }
    }
  };

  const hasExactlyKeys = (value, expected) => {
    const keys = Object.keys(value);
    return keys.length === expected.length && expected.every((key) => keys.includes(key));
  };

  const validateEventArguments = (command, args) => {
    if (command === EVENT_LISTEN_COMMAND) {
      if (
        !hasExactlyKeys(args, ["event", "target", "handler"])
        || args.event !== NAVIGATION_EVENT
        || !isPlainRecord(args.target)
        || !hasExactlyKeys(args.target, ["kind"])
        || args.target.kind !== "Any"
        || !Number.isSafeInteger(args.handler)
        || args.handler < 0
      ) {
        throw new Error("OpenChronicle blocked an invalid event listener");
      }
      return;
    }

    if (
      !hasExactlyKeys(args, ["event", "eventId"])
      || args.event !== NAVIGATION_EVENT
      || !Number.isSafeInteger(args.eventId)
      || args.eventId < 0
    ) {
      throw new Error("OpenChronicle blocked an invalid event unlistener");
    }
  };

  window.__TAURI_ISOLATION_HOOK__ = (payload) => {
    if (
      !isPlainRecord(payload)
      || !hasExactlyKeys(payload, ["cmd", "callback", "error", "payload", "options"])
      || typeof payload.cmd !== "string"
      || !Number.isSafeInteger(payload.callback)
      || payload.callback < 0
      || !Number.isSafeInteger(payload.error)
      || payload.error < 0
      || (payload.options !== undefined
        && payload.options !== null
        && (!isPlainRecord(payload.options) || Object.keys(payload.options).length !== 0))
    ) {
      throw new Error("OpenChronicle blocked an invalid command envelope");
    }

    const isCustomCommand = ALLOWED_COMMANDS.has(payload.cmd);
    const isEventCommand =
      payload.cmd === EVENT_LISTEN_COMMAND || payload.cmd === EVENT_UNLISTEN_COMMAND;
    if (!isCustomCommand && !isEventCommand) {
      throw new Error("OpenChronicle blocked an unknown command");
    }

    const args = payload.payload;
    if (!isPlainRecord(args)) {
      throw new Error("OpenChronicle command arguments must be an object");
    }
    let inspected;
    if (isCustomCommand) {
      if (!hasExactlyKeys(args, ["request"]) || !isPlainRecord(args.request)) {
        throw new Error("OpenChronicle command arguments must contain only request");
      }
      inspected = args.request;
    } else {
      validateEventArguments(payload.cmd, args);
      inspected = args;
    }

    inspectValue(inspected);
    const encoded = new TextEncoder().encode(JSON.stringify(inspected));
    if (encoded.byteLength > MAX_REQUEST_BYTES) {
      throw new Error("OpenChronicle request exceeds the size limit");
    }
    return payload;
  };
})();
