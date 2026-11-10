"""Lexical (keyword) search shared by index-corpus and analyze.

Both Lambdas must tokenize text exactly the same way, so build.sh copies this
single file into both packages. Changing anything here requires bumping
TOKENIZER_VERSION and re-indexing every corpus layer.
"""
import math
import re
import threading
import unicodedata

import numpy as np
import snowballstemmer

TOKENIZER_VERSION = "es-snowball-v1"

STOPWORDS = {
    "a", "al", "algo", "como", "con", "cual", "de", "del", "desde", "donde", "e", "el", "ella", "ellos",
    "en", "entre", "era", "es", "esa", "ese", "eso", "esta", "este", "esto", "fue", "ha", "han", "hasta",
    "la", "las", "le", "les", "lo", "los", "mas", "me", "mi", "muy", "ni", "no", "nos", "o", "otra", "otro",
    "para", "pero", "por", "que", "se", "sea", "ser", "si", "sin", "sobre", "son", "su", "sus", "tambien",
    "te", "tiene", "todo", "u", "un", "una", "uno", "y", "ya",
}
PSEUDONYM_TOKEN = re.compile(r"\[[A-Z_]+_\d+\]")
WORD = re.compile(r"[a-záéíóúüñ0-9]+")

# Snowball stemmers keep internal state between calls, so each thread gets its own
_local = threading.local()


def _stem(word):
    if not hasattr(_local, "stemmer"):
        _local.stemmer = snowballstemmer.stemmer("spanish")
    return _local.stemmer.stemWord(word)


def strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def tokenize(text):
    """Lowercase words, without pseudonymization tokens or stopwords, reduced to their stem."""
    words = WORD.findall(PSEUDONYM_TOKEN.sub(" ", text).lower())
    tokens = []
    for word in words:
        if len(word) < 2 or strip_accents(word) in STOPWORDS:
            continue
        # Stem before removing accents: the Spanish stemmer's suffix rules include accents
        tokens.append(strip_accents(word if word.isdigit() else _stem(word)))
    return tokens


def build_lexical_index(texts):
    """Postings list for BM25: for every term, the chunks that contain it and how many times."""
    postings, doc_lengths = {}, []
    for i, text in enumerate(texts):
        terms = tokenize(text)
        doc_lengths.append(len(terms))
        counts = {}
        for term in terms:
            counts[term] = counts.get(term, 0) + 1
        for term, tf in counts.items():
            postings.setdefault(term, []).append([i, tf])
    return {"tokenizer": TOKENIZER_VERSION, "doc_lengths": doc_lengths, "postings": postings}


def bm25_scores(lexical_index, query_text, k1=1.2, b=0.75):
    """One BM25 score per chunk (0 for chunks that share no term with the query)."""
    lengths = np.asarray(lexical_index["doc_lengths"], dtype=np.float32)
    n = len(lengths)
    scores = np.zeros(n, dtype=np.float32)
    if n == 0:
        return scores
    avg_length = max(float(lengths.mean()), 1.0)
    for term in set(tokenize(query_text)):
        docs = lexical_index["postings"].get(term)
        if not docs:
            continue
        idf = math.log(1 + (n - len(docs) + 0.5) / (len(docs) + 0.5))
        idx = np.fromiter((d for d, _ in docs), dtype=np.int64, count=len(docs))
        tf = np.fromiter((f for _, f in docs), dtype=np.float32, count=len(docs))
        scores[idx] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[idx] / avg_length))
    return scores


def reciprocal_rank_fusion(rankings, k=60):
    """Merges several rankings using only positions, so scores on different scales can be combined."""
    fused = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking):
            fused[doc] = fused.get(doc, 0.0) + 1.0 / (k + rank + 1)
    return sorted(fused, key=fused.get, reverse=True)
