# Resume after an interrupted Miro frame

An HTTP 500 during **Updating graph export frames** comes from Miro's frame creation endpoint. Layout has finished, and the publisher has already saved the IDs of acknowledged shapes, connectors, and frames. The response does not tell us whether Miro created the failing frame before returning an error. Repeating its POST immediately could create a duplicate.

Keep the investigation's saved Miro state. You do not need to erase the board, delete the mapping, or trace again.

## Browser recovery

1. Open the investigation in `liquid-web` and select **Recover interrupted frame**.
2. Review the requested frame and open the linked board. Complete any Proton Pass prompt in the launching terminal.
3. If the frame exists, explicitly select its matching frame ID. If no possible match exists, inspect the board and confirm that this specific frame is absent. Confirming absence applies to the frame, not the populated board.
4. Apply recovery. The app checks the frame inventory again and updates only the local mapping. It selects the interrupted snapshot.
5. Choose **Sync to Miro** to finish publication. Previously acknowledged objects keep their saved IDs. Choose **Sync and reorganize** only if you also want to apply the automatic layout again.

Recovery does not create, delete, or move any Miro objects. A successful recovery does not start another sync automatically. Ordinary sync still checks live objects and preserves manual edits under its existing rules.

If several frames exactly match, choose the correct existing ID after inspecting the board. A frame with the same title but different bounds, or matching bounds but a different title, blocks absence confirmation: it may have been manually edited. Restore its identifying properties and review again, or use the existing per-item `miro-resolve` command after identifying it yourself.

Incomplete API reads, changed local state, or changed board inventory invalidate the review. Start a fresh review. Recovery remains available if another frame fails during the next sync.

## Terminal commands

Read the current review:

```bash
liquid-live miro-frame-review --case cases/YOUR_CASE_DIRECTORY
```

Use the returned `review_id` and explicitly choose one returned candidate:

```bash
liquid-live miro-frame-recover --case cases/YOUR_CASE_DIRECTORY \
  --review-id REVIEW_ID --item-id EXISTING_FRAME_ID
```

Alternatively, only when the review permits it and you have inspected the board and verified this frame is absent:

```bash
liquid-live miro-frame-recover --case cases/YOUR_CASE_DIRECTORY \
  --review-id REVIEW_ID --confirm-absent
```

Resume the `run_id` returned by recovery:

```bash
liquid-live miro-sync --case cases/YOUR_CASE_DIRECTORY --run RUN_ID
```

Both recovery commands support `--output NEW_REPORT.json`. These reports contain frame titles and geometry; store them with the investigation.

## Scope and limitations

This recovery handles one uncertain frame creation belonging to the interrupted run, including existing saved failures. Initial empty-board batch recovery and per-item reconciliation remain available for other failures. An empty API result cannot prove that a delayed server request will never complete, so absence always requires the operator's inspection and confirmation.

Miro's documented [frame creation API](https://developers.miro.com/reference/create-frame-item) does not provide a request idempotency key or a way to retrieve an item using the failed request's request ID. The [frame read API](https://developers.miro.com/reference/get-frame-item) and paginated item inventory let the app compare title, canvas position, and dimensions. The app does not assume a graph-size limit or invent a frame-size cap from an HTTP 500. The earlier rate-limit waits are separate from this server error.
