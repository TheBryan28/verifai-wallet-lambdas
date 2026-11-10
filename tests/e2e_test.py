"""End-to-end local test: index-corpus -> extract (segmentation) -> analyze, with fake S3 and Bedrock."""
import hashlib, importlib.util, io, json, os, shutil, sys, tempfile, types
import numpy as np
from botocore.exceptions import ClientError

# Usage: python lambdas/tests/e2e_test.py   (needs boto3, numpy, pypdf and snowballstemmer installed)
W = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SP = tempfile.mkdtemp(prefix="verifai-e2e-")
os.environ.update(AWS_DEFAULT_REGION="eu-south-2", AWS_REGION="eu-south-2", BUCKET="verifai-auditoria",
                  ANALYSES_TABLE="verifai-analyses", MODEL_ID="eu.test-claude", CORPUS_DIR=f"{SP}/corpus-tmp")
shutil.rmtree(f"{SP}/corpus-tmp", ignore_errors=True)
sys.path.insert(0, f"{W}/lambdas/shared")

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

class FakeS3:
    def __init__(self): self.objects = {}
    def _missing(self, op): return ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, op)
    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):
        if IfNoneMatch == "*" and Key in self.objects or IfMatch and self.etag(Key) != IfMatch:
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "x"}}, "PutObject")
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()
    def etag(self, key): return hashlib.md5(self.objects[key]).hexdigest()
    def get_object(self, Bucket, Key):
        if Key not in self.objects: raise self._missing("GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": self.etag(Key)}
    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)], "IsTruncated": False}

VOCAB = ["arrendatario", "terminacion", "penalizacion", "abusiva", "consumidor", "trabajador", "salario", "preaviso"]
def fake_vector(text, dims=1024):
    t = text.lower()
    v = np.array([t.count(w) for w in VOCAB] + [0.01] * (dims - len(VOCAB)), dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()

class FakeBedrock:
    def __init__(self): self.embeds = 0; self.converse_calls = []
    def invoke_model(self, modelId, body, **kw):
        self.embeds += 1
        return {"body": io.BytesIO(json.dumps({"embedding": fake_vector(json.loads(body)["inputText"])}).encode())}
    def converse(self, **req):
        self.converse_calls.append(req)
        prompt = req["messages"][0]["content"][0]["text"]
        import re
        findings = []
        for idx, ids in re.findall(r'<clause index="(\d+)"[^>]*sources="([^"]*)"', prompt):
            first = ids.split(",")[0] if ids else ""
            findings.append({"clause_index": int(idx), "risk": "high" if "penaliz" in prompt else "low",
                             "category": "abusive_clause", "explanation": "e",
                             "citations": [first, "INVENTED-99"] if first else []})
        return {"output": {"message": {"content": [{"toolUse": {"name": "report_findings", "input": {"findings": findings}}}]}},
                "stopReason": "tool_use", "usage": {"inputTokens": 1000, "outputTokens": 200}}

s3, bedrock = FakeS3(), FakeBedrock()

# ---- corpus sources ----
ley820 = """LEY 820 DE 2003
Por la cual se expide el régimen de arrendamiento de vivienda urbana.

ARTÍCULO 1o. OBJETO. La presente ley tiene por objeto fijar los criterios que deben servir de base para regular los contratos de arrendamiento de los inmuebles urbanos destinados a vivienda.

ARTÍCULO 5o. DEROGADO. Artículo derogado por la Ley 1234.

ARTÍCULO 24. TERMINACIÓN POR PARTE DEL ARRENDATARIO. Son causales para que el arrendatario pueda pedir unilateralmente la terminación del contrato, con preaviso de tres meses y pago de una indemnización equivalente al precio de tres meses de arrendamiento.
"""
ley1480 = """LEY 1480 DE 2011
ARTÍCULO 42. CONCEPTO Y PROHIBICIÓN. Son cláusulas abusivas aquellas que producen un desequilibrio injustificado en perjuicio del consumidor.
ARTÍCULO 43. CLÁUSULAS ABUSIVAS INEFICACES DE PLENO DERECHO. Son ineficaces de pleno derecho las cláusulas que impongan al consumidor penalización excesiva.
"""
sentencia = " ".join(["La Corte considera que la penalización desproporcionada al arrendatario vulnera el equilibrio contractual."] * 60)
for name, text, meta in [
    ("ley-820-2003", ley820, {"norm": "Ley 820 de 2003", "type": "law", "url": "https://www.secretariasenado.gov.co/ley-820", "derogated_articles": ["5"]}),
    ("ley-1480-2011", ley1480, {"norm": "Ley 1480 de 2011", "type": "law", "url": "https://www.secretariasenado.gov.co/ley-1480"}),
    ("t-123-2020", sentencia, {"norm": "Corte Constitucional, Sentencia T-123 de 2020", "type": "case_law"}),
]:
    s3.objects[f"corpus-source/CO/2026-11/{name}.txt"] = text.encode()
    s3.objects[f"corpus-source/CO/2026-11/{name}.json"] = json.dumps(meta).encode()

ic = load("index_corpus", f"{W}/lambdas/index-corpus/app.py")
ic.s3, ic.bedrock = s3, bedrock
ctx = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 600_000)
out = ic.lambda_handler({"code": "co", "version": "2026-11", "activate": True}, ctx)
print("INDEX", out)
assert out["chunks"] == 4 + 2 - 0 or out["chunks"] >= 5, out
assert "CO-ley-820-2003-art5" in out["excluded_derogated"]
assert out["embedded_now"] == out["chunks"] and out["activated"]
manifest = json.loads(s3.objects["corpus/manifest.json"])
assert manifest["jurisdictions"]["CO"]["active_version"] == "2026-11" and "ES" in manifest["eu_members"]
chunks = [json.loads(l) for l in s3.objects["corpus/CO/2026-11/chunks.jsonl"].decode().splitlines()]
print("CHUNK IDS", [c["id"] for c in chunks])
idx = np.load(io.BytesIO(s3.objects["corpus/CO/2026-11/index.npy"]))
assert idx.shape == (len(chunks), 1024)
before = bedrock.embeds
out2 = ic.lambda_handler({"code": "CO", "version": "2026-11"}, ctx)
assert bedrock.embeds == before and out2["from_cache"] == out2["chunks"], "cache not used"
try:
    ic.lambda_handler({"code": "ES", "version": "2026-11", "parent": "EU", "activate": True}, ctx); raise SystemExit("should fail")
except ic.SourceNotFound: pass
try:
    ic.lambda_handler({"code": "CO", "version": "2026-11"}, types.SimpleNamespace(get_remaining_time_in_millis=lambda: 1000)); raise SystemExit("should fail")
except ic.TimeBudgetExceeded: pass

# ---- extract segmentation ----
ex = load("extract_app", f"{W}/lambdas/extract/app.py")
filler = " ".join(["texto"] * 60)
page1 = f"CONTRATO DE ARRENDAMIENTO\nEntre las partes, con cédula de ciudadanía No. 79.123.456 y correo ana@example.com.\nPRIMERA.- OBJETO. {filler}\nSEGUNDA.- Corta."
page2 = f"CLÁUSULA 3. PENALIZACIÓN. Si el arrendatario termina antes pagará 24 meses, conforme al art. 24 de la Ley 820 de 2003. {filler}\n" \
        f"Artículo 4. LARGA. " + "\n".join(" ".join(["palabra"] * 100) for _ in range(10)) + \
        f"\n5. Notificaciones. {filler}\n5.1 Domicilio. {filler}\nel artículo 1602 del Código Civil aplica. {filler}"
p = ex.Pseudonymizer()
pages = [p.apply(ex.normalize_whitespace(x)) for x in (page1, page2)]
assert "79.123.456" not in pages[0] and "ana@example.com" not in pages[0], pages[0][:200]
clauses = ex.split_into_clauses(pages)
for c in clauses: print("CLAUSE", c["index"], c["page"], c["word_count"], "|", c["heading"][:50])
heads = [c["heading"] for c in clauses]
assert any(h.startswith("PRIMERA") for h in heads) and not any(h.startswith("SEGUNDA") for h in heads), "short clause not merged"
assert all(c["word_count"] <= 800 for c in clauses) and any("(part 2)" in h for h in heads)
assert [c for c in clauses if c["heading"].startswith("CLÁUSULA 3")][0]["page"] == 2
assert not any(h.lower().startswith("el artículo 1602") for h in heads), "prose line taken as heading"
blocks = ex.split_into_clauses([" ".join(["libre"] * 1000)])
print("FALLBACK", [(b["heading"], b["word_count"]) for b in blocks])
assert len(blocks) == 3 and all(b["word_count"] <= 400 for b in blocks)

# ---- analyze ----
an = load("analyze_app", f"{W}/lambdas/analyze/app.py")
an.s3, an.bedrock = s3, bedrock
s3.objects["processed/abc/text.json"] = json.dumps({"country": "CO", "clauses": clauses}).encode()
result = an.lambda_handler({"analysis_id": "abc", "bucket": "verifai-auditoria", "processed_key": "processed/abc/text.json"}, None)
report = json.loads(s3.objects[result["report_key"]])
print("RESULT", result, "llm_calls", report["usage"]["llm_calls"], "summary", report["summary"])
assert report["usage"]["llm_calls"] == 1, "expected one Bedrock call per contract"
assert result["corpus_version"] == "CO@2026-11"
pen = [f for f in report["findings"] if f["clause_heading"].startswith("CLÁUSULA 3")][0]
print("PENALTY FINDING", json.dumps(pen, ensure_ascii=False))
assert all(c["source_id"] != "INVENTED-99" for f in report["findings"] for c in f["citations"])
srcs = an.retrieve_sources(clauses[[c["heading"] for c in clauses].index(pen["clause_heading"])]["text"], [an._layer_cache["corpus/CO/2026-11/"]])
print("PENALTY SOURCES", [(s["id"], s["match"]) for s in srcs])
assert srcs[0]["id"] == "CO-ley-820-2003-art24" and srcs[0]["match"] == "reference"
assert pen["page"] == 2
prompt = bedrock.converse_calls[-1]["messages"][0]["content"][0]["text"]
assert prompt.count('<source id="CO-ley-820-2003-art24"') == 1, "sources must be deduplicated"
# CLAUSES_PER_CALL split + max_tokens split
an.CLAUSES_PER_CALL = 2
n_before = len(bedrock.converse_calls)
an.lambda_handler({"analysis_id": "abc", "bucket": "verifai-auditoria", "processed_key": "processed/abc/text.json"}, None)
assert len(bedrock.converse_calls) - n_before == -(-len(clauses) // 2)
print("ALL END-TO-END TESTS PASSED")
