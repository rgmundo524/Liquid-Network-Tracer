type Frame = { title: string; x: number; y: number; width: number; height: number };
type Candidate = Frame & { id: string };
export type FrameRecoveryReview = {
  schema_version: 1;
  recovery: "pending_frame_review";
  review_id: string;
  run_id: string;
  pending_frame: Frame;
  candidates: Candidate[];
  potential_match_count: number;
  can_confirm_absent: boolean;
};
type Approval = { review_id: string; item_id: string } | { review_id: string; confirm_absent: true };

let generation = 0;
let owner = "";
let pendingJob = "";
let review: FrameRecoveryReview | null = null;

const esc = (value: unknown): string => String(value ?? "").replace(/[&<>"']/g,
  character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]!);
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const validFrame = (value: unknown): value is Frame => object(value) && typeof value.title === "string" &&
  [value.x, value.y, value.width, value.height].every(number => typeof number === "number" && Number.isFinite(number)) &&
  (value.width as number) > 0 && (value.height as number) > 0;

/** A review belongs to one page visit and one read job, never to a restored or older job. */
export function resetFrameRecovery(): void {
  generation += 1; owner = ""; pendingJob = ""; review = null;
}

export function beginFrameRecovery(caseId: string): number {
  resetFrameRecovery(); owner = caseId; return generation;
}

export function frameRecoveryStarted(caseId: string, version: number, jobId: string): void {
  if (owner === caseId && generation === version) pendingJob = jobId;
}

export function frameRecoveryComplete(caseId: string, jobId: string, result: unknown): boolean {
  if (owner !== caseId || !pendingJob || pendingJob !== jobId) return false;
  pendingJob = ""; review = null;
  if (!object(result) || result.schema_version !== 1 || result.recovery !== "pending_frame_review" ||
      typeof result.review_id !== "string" || !/^[0-9a-f]{64}$/.test(result.review_id) ||
      typeof result.run_id !== "string" || !result.run_id || !validFrame(result.pending_frame) ||
      !Array.isArray(result.candidates) ||
      !result.candidates.every(candidate => object(candidate) && typeof candidate.id === "string" && /^[A-Za-z0-9_][A-Za-z0-9_-]{0,199}$/.test(candidate.id) && validFrame(candidate)) ||
      new Set(result.candidates.map(candidate => candidate.id)).size !== result.candidates.length ||
      !Number.isSafeInteger(result.potential_match_count) || (result.potential_match_count as number) < 0 ||
      typeof result.can_confirm_absent !== "boolean" ||
      (result.can_confirm_absent && (result.candidates.length > 0 || (result.potential_match_count as number) > 0))) {
    throw new Error("The frame review is incomplete. Review the interrupted frame again before recovering.");
  }
  review = structuredClone(result) as FrameRecoveryReview;
  return true;
}

/** Consume the approval before submitting so errors cannot leave a reusable confirmation. */
export function frameRecoveryApproval(caseId: string, itemId: string, confirmAbsent: boolean): Approval {
  const current = owner === caseId ? review : null;
  if (!current) throw new Error("Review the interrupted frame again before recovering.");
  if (itemId && !confirmAbsent && current.candidates.some(candidate => candidate.id === itemId)) {
    resetFrameRecovery(); return { review_id: current.review_id, item_id: itemId };
  }
  if (!itemId && confirmAbsent && current.can_confirm_absent && current.candidates.length === 0 && current.potential_match_count === 0) {
    resetFrameRecovery(); return { review_id: current.review_id, confirm_absent: true };
  }
  throw new Error(current.candidates.length ? "Select the existing frame you inspected on the linked board." : "Inspect the linked board and confirm that the interrupted frame is absent.");
}

const measurements = (frame: Frame): string => `Center (${frame.x}, ${frame.y}); size ${frame.width} × ${frame.height}`;

export function frameRecoveryDialog(caseId: string, boardUrl: string): string {
  if (owner !== caseId || !review) return "";
  // Use the currently linked board supplied by the application, never a URL from the review payload.
  let linked: URL;
  try { linked = new URL(boardUrl); } catch { return ""; }
  if (linked.origin !== "https://miro.com" || !linked.pathname.startsWith("/app/board/") || linked.username || linked.password) return "";
  const frame = review.pending_frame;
  const blocked = review.candidates.length === 0 && !review.can_confirm_absent;
  return `<form id="frame-recovery-form"><header class="dialog-head"><div><h2 id="dialog-title">Recover interrupted frame</h2><p>Review the frame whose creation result was not confirmed.</p></div><button type="button" class="dialog-close" data-action="close-dialog" aria-label="Close dialog">×</button></header>
    <div class="dialog-body"><p>The graph objects already created have saved IDs. Recovery reads Miro and updates the local record for this frame. Then choose Sync to Miro to resume the interrupted snapshot.</p>
    <div class="dialog-board"><span>Linked board</span><a class="board-link" href="${esc(linked.href)}" target="_blank" rel="noopener noreferrer">Open linked board</a><span>Interrupted snapshot</span><strong class="mono">${esc(review.run_id)}</strong><span>Expected frame</span><strong>${esc(frame.title)}</strong><span>${esc(measurements(frame))}</span></div>
    ${review.candidates.length ? `<fieldset><legend>Choose the existing frame you inspected</legend><p>These frames match the expected title, position, and size. Nothing is selected automatically.</p>${review.candidates.map(candidate => `<label class="check-line"><input type="radio" name="frame_item_id" value="${esc(candidate.id)}" required/><span><strong>${esc(candidate.title)}</strong><small>Frame ID: ${esc(candidate.id)}<br>${esc(measurements(candidate))}</small></span></label>`).join("")}</fieldset>` : review.can_confirm_absent ? `<p>No matching frame was found. This does not prove the creation failed. Inspect the linked board before confirming that the frame is absent.</p><label class="check-line"><input type="checkbox" name="confirm_frame_absent" required/><span><strong>I inspected this board after the failed sync and the expected frame is absent.</strong><small>If it exists with a different title or position, cancel and reconcile that existing frame instead.</small></span></label>` : `<div class="alert warning"><div><strong>Inspect the possible existing frame${review.potential_match_count === 1 ? "" : "s"}</strong><p>${esc(review.potential_match_count)} frame${review.potential_match_count === 1 ? " matches" : "s match"} only the expected title or position and size. The interrupted frame may have been edited. Restore its expected title and layout, then review again, or reconcile it with miro-resolve. Absence cannot be confirmed from this review.</p></div></div>`}
    <p class="small muted">Miro is checked again before recovery is saved. Complete any Proton Pass prompt in the launching terminal. Recovery does not move, create, or delete board objects.</p></div>
    <footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button>${blocked ? "" : `<button type="submit" class="btn primary">${review.candidates.length ? "Use selected existing frame" : "Confirm absent frame"}</button>`}</footer></form>`;
}
