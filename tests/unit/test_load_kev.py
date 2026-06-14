from scripts.load_kev import run, upsert_records


class _FakeKevConn:
    """Simulates the upsert's RETURNING (xmax = 0): a cve_id already present is an
    update (False); a new one is an insert (True), matching Postgres' behavior."""

    def __init__(self, existing=()):
        self.existing = set(existing)

    async def fetchval(self, _sql, *args):
        cve_id = args[0]  # first INSERT column is cve_id
        if cve_id in self.existing:
            return False
        self.existing.add(cve_id)
        return True

    async def close(self):
        pass


async def test_upsert_records_counts_new_and_modified():
    vulns = [{"cveID": "CVE-1"}, {"cveID": "CVE-2"}, {"cveID": "CVE-3"}]
    embeddings = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
    conn = _FakeKevConn(existing={"CVE-2"})  # one already present

    counts = await upsert_records(conn, vulns, embeddings)

    assert counts == {"new": 2, "modified": 1}


async def test_run_reports_new_and_modified(monkeypatch):
    """run() threads the upsert delta into both the summary and metrics."""
    vulns = [{"cveID": "CVE-1"}, {"cveID": "CVE-2"}, {"cveID": "CVE-3"}]

    async def fake_fetch():
        return vulns

    async def fake_embeddings(_client, texts):
        return [[0.0, 0.0] for _ in texts]

    async def fake_connect(*_args, **_kwargs):
        return _FakeKevConn(existing={"CVE-2"})

    async def fake_register_vector(_conn):
        pass

    import scripts.load_kev as load_kev

    monkeypatch.setattr(load_kev, "fetch_kev_data", fake_fetch)
    monkeypatch.setattr(load_kev, "generate_embeddings", fake_embeddings)
    monkeypatch.setattr(load_kev.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(load_kev, "register_vector", fake_register_vector)

    report = await run()

    assert report.metrics == {"fetched": 3, "new": 2, "modified": 1, "loaded": 3}
    assert report.summary == "Loaded 3 KEV records (2 new, 1 modified)"
