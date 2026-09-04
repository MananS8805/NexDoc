"""Real, model-backed Q&A over the common schema — as opposed to
`eval/run_eval.py`'s deterministic fact-lookup, this package actually calls
an LLM (Groq) to answer a question given a representation of an artifact.
See `qa/groq_qa.py` for the one function every caller (eval script, web
app) shares.
"""
