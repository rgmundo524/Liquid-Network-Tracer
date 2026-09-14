from pathlib import Path
import hashlib
root = Path('.')
expected = {'liquid_tracer/cli.py': '297149546031e3a5f9da5e76ea2845baf87bd428', 'liquid_tracer/elk_layout.py': 'c19a9355b1c24a9a1c5b7e04c4117ceb49dfd18f', 'liquid_tracer/progress.py': '561bbf98ccadc57800f22bad4865b25a36994bb1'}
for name, sha in expected.items():
    raw = (root / name).read_bytes()
    assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == sha, name

def replace(file, old, new):
    p = root / file
    text = p.read_text()
    assert text.count(old) == 1, (file, old, text.count(old))
    p.write_text(text.replace(old, new))

replace('liquid_tracer/cli.py','                         service_settings=None):','                         service_settings=None, preview_directory=None):')
replace('liquid_tracer/cli.py', '    refreshed = make_plan(optimize_graph(graph, connector_style=connector_style, progress=progress))', '    from .layout_reuse import reusable_elk_preview, report_phase\n    laid_out = reusable_elk_preview(graph, preview_directory, connector_style, progress)\n    if laid_out is None:\n        laid_out = optimize_graph(graph, connector_style=connector_style, progress=progress)\n    report_phase(progress, "building_plan")\n    refreshed = make_plan(laid_out)')
replace('liquid_tracer/cli.py','                                    service_settings=load_services(case))','                                    service_settings=load_services(case), preview_directory=Path(case) / "previews")')
replace('liquid_tracer/progress.py','    "layout": "Preparing the graph layout",','    "layout": "Preparing the graph layout",\n    "checking_layout": "Checking completed ELK previews for this full graph",\n    "reusing_layout": "Reusing the completed ELK layout; no recalculation",\n    "building_plan": "Building and validating the Miro publication plan",')
replace('liquid_tracer/progress.py','\n\ndef public_progress(event):','\n\nELK_STAGES = {\n    "preparing": "Preparing the graph for ELK",\n    "measuring_input": "Measuring the input layout before ELK",\n    "calculating": "Calculating the graph layout with ELK",\n    "applying": "Validating ELK coordinates and connector routes",\n    "measuring_output": "Measuring the completed ELK layout",\n    "ready": "ELK layout completed",\n}\n\n\ndef public_progress(event):')
replace('liquid_tracer/progress.py','    if value["phase"] == "waiting":', '''    if value["phase"] == "optimizing":
        stage = event.get("stage")
        if isinstance(stage, str) and stage in ELK_STAGES:
            value.update(stage=stage, message=ELK_STAGES[stage])
        for field in ("node_count", "edge_count", "heap_mb"):
            number = event.get(field)
            if type(number) is int and 0 <= number <= 2 ** 53 - 1:
                value[field] = number
        if "node_count" in value and "edge_count" in value:
            value["message"] += f" ({value['node_count']:,} objects, {value['edge_count']:,} connections)"
        if "heap_mb" in value:
            value["message"] += f"; Node heap budget {value['heap_mb']:,} MiB"
    if value["phase"] == "waiting":''')
replace('liquid_tracer/progress.py','        self.previous_phase = None','        self.previous_phase = None\n        self.previous_stage = None')
replace('liquid_tracer/progress.py','        urgent = (value["phase"] != self.previous_phase or value["phase"] == "waiting"','        urgent = (value["phase"] != self.previous_phase or value.get("stage") != self.previous_stage\n                  or value["phase"] == "waiting"')
replace('liquid_tracer/progress.py','        self.previous_phase = value["phase"]','        self.previous_phase = value["phase"]\n        self.previous_stage = value.get("stage")')
replace('liquid_tracer/elk_layout.py','def _report_progress(progress, message, *, completed=0, elapsed_seconds=None):','def _report_progress(progress, message, *, completed=0, elapsed_seconds=None, stage=None, **counts):')
replace('liquid_tracer/elk_layout.py','        if elapsed_seconds is not None:\n            event["elapsed_seconds"] = elapsed_seconds','        if stage is not None:\n            event["stage"] = stage\n        event.update(counts)\n        if elapsed_seconds is not None:\n            event["elapsed_seconds"] = elapsed_seconds')
replace('liquid_tracer/elk_layout.py','        _report_progress(progress, f"Calculating local ELK layout ({context}); cancel to stop")','        _report_progress(progress, f"Calculating local ELK layout ({context}); cancel to stop",\n                         stage="calculating", node_count=node_count, edge_count=edge_count, heap_mb=heap_mb)')
replace('liquid_tracer/elk_layout.py','                                 elapsed_seconds=elapsed)','                                 elapsed_seconds=elapsed, stage="calculating",\n                                 node_count=node_count, edge_count=edge_count, heap_mb=heap_mb)')
replace('liquid_tracer/elk_layout.py','    _report_progress(progress, "Calculating local ELK layout; cancel to stop")','    _report_progress(progress, "Calculating local ELK layout; cancel to stop", stage="preparing",\n                     node_count=len(graph["nodes"]), edge_count=len(graph["edges"]))')
replace('liquid_tracer/elk_layout.py','    before = layout_metrics(graph)','    _report_progress(progress, "Measuring input layout", stage="measuring_input")\n    before = layout_metrics(graph)')
replace('liquid_tracer/elk_layout.py','    for candidate in candidates:\n        try:','    for candidate in candidates:\n        _report_progress(progress, "Validating ELK coordinates and routes", stage="applying")\n        try:')
replace('liquid_tracer/elk_layout.py','        metrics = layout_metrics(result)','        _report_progress(progress, "Measuring completed ELK layout", stage="measuring_output")\n        metrics = layout_metrics(result)')
replace('liquid_tracer/elk_layout.py','    _report_progress(progress, "Local ELK layout ready", completed=1)','    _report_progress(progress, "Local ELK layout ready", completed=1, stage="ready")')
