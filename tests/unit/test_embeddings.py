from unittest.mock import AsyncMock, MagicMock

from rag.embeddings import generate_embedding, generate_embeddings_batch

_EMBEDDING = [0.1] * 1536


def _make_openai_mock(embeddings: list[list[float]]) -> AsyncMock:
    client = AsyncMock()
    data = []
    for emb in embeddings:
        item = MagicMock()
        item.embedding = emb
        data.append(item)
    response = MagicMock()
    response.data = data
    client.embeddings.create = AsyncMock(return_value=response)
    return client


async def test_generate_embedding_returns_float_list(mock_settings):
    client = _make_openai_mock([_EMBEDDING])
    result = await generate_embedding(client, "test text")
    assert result == _EMBEDDING


async def test_generate_embeddings_batch_calls_api_once_returns_three_vectors(mock_settings):
    embeddings = [[0.1] * 1536, [0.2] * 1536, [0.3] * 1536]
    client = _make_openai_mock(embeddings)
    result = await generate_embeddings_batch(client, ["a", "b", "c"])
    client.embeddings.create.assert_called_once()
    assert len(result) == 3
    assert result[0] == embeddings[0]
    assert result[1] == embeddings[1]
    assert result[2] == embeddings[2]


async def test_generate_embeddings_batch_empty_list_skips_api(mock_settings):
    client = _make_openai_mock([])
    result = await generate_embeddings_batch(client, [])
    client.embeddings.create.assert_not_called()
    assert result == []
