---

title: Chroma Integration Guide
type: document
space: concept
tags: [concept]
created: 2026-05-20
updated: 2026-05-20
links: []
links:
  - "[[P4-cortex/knowledge/NEURONFS_RULES]]"
---


# Chroma Integration Guide

Integration with LangChain, LlamaIndex, and frameworks.

## LangChain

```python
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

vectorstore = Chroma.from_documents(
    documents=docs,
    embedding=OpenAIEmbeddings(),
    persist_directory="./chroma_db"
)

# Query
results = vectorstore.similarity_search("query", k=3)

# As retriever
retriever = vectorstore.as_retriever()
```

## LlamaIndex

```python
from llama_index.vector_stores.chroma import ChromaVectorStore
import chromadb

db = chromadb.PersistentClient(path="./chroma_db")
collection = db.get_or_create_collection("docs")

vector_store = ChromaVectorStore(chroma_collection=collection)
```

## Resources

- **Docs**: https://docs.trychroma.com
