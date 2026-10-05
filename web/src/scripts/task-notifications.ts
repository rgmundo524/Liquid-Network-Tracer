type NotificationHost = {
  Notification?: typeof Notification;
  localStorage?: Pick<Storage, "getItem" | "setItem">;
  isSecureContext?: boolean;
  focus?: () => void;
};

type TaskResult = {
  id: string;
  status: "succeeded" | "failed" | "canceled";
  investigation: string;
  action: string;
  open: () => void;
};

const preferenceKey = "liquid-tracer:system-notifications:v1";
const seenKey = "liquid-tracer:task-notifications:v1";
const rememberedLimit = 256;

export function createTaskNotifications(host: NotificationHost) {
  const read = (key: string): string | null => {
    try { return host.localStorage?.getItem(key) ?? null; } catch { return null; }
  };
  const write = (key: string, value: string): void => {
    try { host.localStorage?.setItem(key, value); } catch { /* Private browsing can deny storage. */ }
  };
  const savedIds = (): string[] => {
    try {
      const ids: unknown = JSON.parse(read(seenKey) || "[]");
      return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string").slice(-rememberedLimit) : [];
    } catch { return []; }
  };
  const seen = new Set(savedIds());
  let enabled = read(preferenceKey) === "on";
  let pending = false;
  let deliveryUnavailable = false;
  const supported = () => Boolean(host.Notification && host.isSecureContext !== false && !deliveryUnavailable);
  const setEnabled = (value: boolean) => { enabled = value; write(preferenceKey, value ? "on" : "off"); };
  function remember(id: string): boolean {
    // Re-read to also suppress results already shown by another browser tab.
    for (const previous of savedIds()) seen.add(previous);
    if (seen.has(id)) return false;
    seen.add(id);
    const recent = [...seen].slice(-rememberedLimit);
    seen.clear(); recent.forEach(value => seen.add(value));
    write(seenKey, JSON.stringify(recent));
    return true;
  }
  return {
    control() {
      if (!supported()) return {label: "Notifications unavailable", hint: "System notifications are unavailable in this browser. In-app notifications still appear.", enabled: false, disabled: true};
      if (pending) return {label: "Allow notifications…", hint: "Respond to the browser's notification permission prompt.", enabled: false, disabled: true};
      if (host.Notification!.permission === "denied") return {label: "Notifications blocked", hint: "Allow notifications for this local site in your browser's site permissions, then enable them here.", enabled: false, disabled: false};
      const active = enabled && host.Notification!.permission === "granted";
      return {label: `Notifications ${active ? "on" : "off"}`, hint: active ? "Turn off system task notifications." : "Enable system notifications when tasks finish, fail, or are canceled.", enabled: active, disabled: false};
    },
    async toggle(): Promise<string> {
      if (!supported()) return "System notifications are unavailable in this browser. In-app notifications still appear.";
      if (pending) return "Respond to the browser's notification permission prompt.";
      if (host.Notification!.permission === "denied") {
        setEnabled(false);
        return "Notifications are blocked. Allow notifications for this local site in your browser's site permissions, then enable them here.";
      }
      if (enabled && host.Notification!.permission === "granted") {
        setEnabled(false);
        return "System notifications turned off. In-app notifications still appear.";
      }
      pending = true;
      try {
        // Called directly from the Enable button's click, never during startup or polling.
        const permission = host.Notification!.permission === "granted" ? "granted" : await host.Notification!.requestPermission();
        setEnabled(permission === "granted");
        return permission === "granted" ? "System notifications enabled for task results. Keep this browser tab open to receive them."
          : permission === "denied" ? "Notifications are blocked. Allow them in your browser's site permissions, then enable them here."
          : "System notifications were not enabled. You can enable them later from the header.";
      } catch {
        setEnabled(false);
        return "The browser could not enable system notifications. Check this site's notification permissions. In-app notifications still appear.";
      } finally { pending = false; }
    },
    remember,
    notify(result: TaskResult): boolean {
      if (!remember(result.id) || !enabled || !supported() || host.Notification!.permission !== "granted") return false;
      const outcome = {succeeded: "Task finished", failed: "Task failed", canceled: "Task canceled"}[result.status];
      try {
        const notification = new host.Notification!(`Liquid Tracer · ${outcome}`, {
          body: `${result.investigation.slice(0, 120)}\n${result.action.slice(0, 80)}. Open Liquid Tracer to review.`,
          tag: `liquid-tracer-task-${result.id}`,
        });
        notification.onclick = () => {
          notification.close();
          host.focus?.();
          result.open();
        };
        return true;
      } catch {
        // Some browsers expose the API but cannot create desktop notifications.
        deliveryUnavailable = true;
        setEnabled(false);
        return false;
      }
    },
  };
}
