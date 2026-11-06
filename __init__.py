from .nodes import SequenceCraftExtension, ImagePresetSequencePickerNode

WEB_DIRECTORY = "./js"


async def comfy_entrypoint() -> SequenceCraftExtension:
    return SequenceCraftExtension()


try:
    from aiohttp import web
    from server import PromptServer

    @PromptServer.instance.routes.post("/sequencecraft/reset")
    async def reset_counter(request):
        data = await request.json()
        node_id = str(data.get("node_id", "default"))
        ImagePresetSequencePickerNode._counters[node_id] = 0
        return web.json_response({"status": "ok", "counter": 0})

    @PromptServer.instance.routes.get("/sequencecraft/counter")
    async def get_counter(request):
        node_id = str(request.rel_url.query.get("node_id", "default"))
        counter = ImagePresetSequencePickerNode._counters.get(node_id, 0)
        return web.json_response({"counter": counter})

    # ---- Editor helpers (syntax check / wildcard browser / preview) ----
    from . import parser
    from .nodes import _WILDCARDS_DIR

    _MAX_TEXT = 20000
    _MAX_EXPAND = 10

    async def _text_body(request):
        data = await request.json()
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or len(text) > _MAX_TEXT:
            raise web.HTTPBadRequest(text="text must be a string up to 20000 chars")
        return data, text

    @PromptServer.instance.routes.post("/sequencecraft/validate")
    async def validate(request):
        _, text = await _text_body(request)
        return web.json_response({"problems": parser.diagnose(text, _WILDCARDS_DIR)})

    @PromptServer.instance.routes.post("/sequencecraft/expand")
    async def expand(request):
        data, text = await _text_body(request)
        decl = data.get("declarations", "")
        seed, count = data.get("seed", 0), data.get("count", 1)
        if not isinstance(decl, str) or len(decl) > _MAX_TEXT or not isinstance(seed, int) \
                or not isinstance(count, int) or not 1 <= count <= _MAX_EXPAND:
            raise web.HTTPBadRequest(text="invalid declarations/seed/count")
        results = [parser.generate(decl + text, seed + i, _WILDCARDS_DIR) for i in range(count)]
        return web.json_response({"results": results})

    @PromptServer.instance.routes.get("/sequencecraft/wildcards")
    async def wildcard_list(request):
        return web.json_response({"names": parser.list_wildcards(_WILDCARDS_DIR)})

    @PromptServer.instance.routes.get("/sequencecraft/wildcard")
    async def wildcard_values(request):
        name = request.rel_url.query.get("name", "")
        if not parser.safe_wildcard_name(name):
            raise web.HTTPBadRequest(text="invalid name")
        values = parser.find_wildcard(name, _WILDCARDS_DIR)
        if not values:
            raise web.HTTPNotFound(text="no such wildcard")
        return web.json_response({"name": name, "values": values})

    # ---- Wildcard files: edited a whole file at a time (see wildcard_store) ----
    from . import wildcard_store as _ws

    def _store_call(fn, *args):
        try:
            return web.json_response(fn(_WILDCARDS_DIR, *args))
        except _ws.Conflict as e:
            return web.json_response({"error": str(e), "current": e.current}, status=409)
        except _ws.NotFound as e:
            return web.json_response({"error": str(e)}, status=404)
        except _ws.StoreError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _store_body(request):
        try:
            data = await request.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="invalid body")
        return data

    @PromptServer.instance.routes.get("/sequencecraft/wildcard-files")
    async def wildcard_files(request):
        """[{path, size, version, entries: [{name, count}], error?}] for every wildcard file."""
        return web.json_response({"files": _ws.list_files(_WILDCARDS_DIR)})

    @PromptServer.instance.routes.get("/sequencecraft/wildcard-file")
    async def wildcard_file_read(request):
        """?path=clothing.yaml → {path, text, version, entries, error?}"""
        return _store_call(_ws.read_file, request.rel_url.query.get("path", ""))

    @PromptServer.instance.routes.post("/sequencecraft/wildcard-file")
    async def wildcard_file_save(request):
        """{path, text} creates; {path, text, base: <version>} overwrites. 409 when base is stale."""
        d = await _store_body(request)
        return _store_call(_ws.save_file, d.get("path"), d.get("text"), d.get("base"))

    @PromptServer.instance.routes.post("/sequencecraft/wildcard-file/rename")
    async def wildcard_file_rename(request):
        """{path, to, base}"""
        d = await _store_body(request)
        return _store_call(_ws.rename_file, d.get("path"), d.get("to"), d.get("base"))

    @PromptServer.instance.routes.post("/sequencecraft/wildcard-file/delete")
    async def wildcard_file_delete(request):
        """{path, base} — the file is kept as <path>.bak"""
        d = await _store_body(request)
        return _store_call(_ws.delete_file, d.get("path"), d.get("base"))

    # ---- Snippets: saved prompt fragments, pasted into prompts by editors ----
    from pathlib import Path

    _SNIPPETS_DIR = Path(__file__).parent / "snippets"

    def _snippet_path(name):
        if not parser.safe_wildcard_name(name):
            raise web.HTTPBadRequest(text="invalid name")
        return _SNIPPETS_DIR / (name.replace("\\", "/") + ".txt")

    @PromptServer.instance.routes.get("/sequencecraft/snippets")
    async def snippet_list(request):
        names = []
        if _SNIPPETS_DIR.is_dir():
            names = sorted((p.relative_to(_SNIPPETS_DIR).with_suffix("").as_posix()
                            for p in _SNIPPETS_DIR.rglob("*.txt") if p.is_file()), key=str.lower)
        return web.json_response({"names": names})

    @PromptServer.instance.routes.get("/sequencecraft/snippet")
    async def snippet_get(request):
        path = _snippet_path(request.rel_url.query.get("name", ""))
        if not path.is_file():
            raise web.HTTPNotFound(text="no such snippet")
        return web.json_response({"text": path.read_text(encoding="utf-8")})

    @PromptServer.instance.routes.post("/sequencecraft/snippet")
    async def snippet_save(request):
        """{name, text} saves (the previous version is kept as .txt.bak); {name, delete: true} removes."""
        data = await request.json()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="invalid body")
        path = _snippet_path(data.get("name", ""))
        if data.get("delete") is True:
            if not path.is_file():
                raise web.HTTPNotFound(text="no such snippet")
            path.replace(path.with_suffix(".txt.bak"))
            return web.json_response({"ok": True})
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT:
            raise web.HTTPBadRequest(text="text must be 1-20000 chars")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            path.replace(path.with_suffix(".txt.bak"))
        path.write_text(text, encoding="utf-8")
        return web.json_response({"ok": True})

except Exception as e:
    print(f"[SequenceCraft] Warning: Could not register API routes: {e}")
