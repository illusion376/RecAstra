import unittest
import os
from unittest.mock import AsyncMock, patch
import httpx
from app.config import Settings
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.llm.base import LLMFatalError

class RetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {key: "" for key in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy")}))

    async def check_failure(self, result):
        provider = OpenAICompatProvider(Settings(llm_max_retries=3))
        await provider._client.aclose()
        provider._client = AsyncMock()
        if isinstance(result, Exception):
            provider._client.post.side_effect = result
        else:
            provider._client.post.return_value = result
        with self.assertRaises(LLMFatalError):
            await provider.complete_json('system', 'user')
        self.assertEqual(provider._client.post.call_count, 1)
        return provider

    async def test_read_timeout_is_not_retried(self):
        await self.check_failure(httpx.ReadTimeout(''))

    async def test_paid_invalid_json_is_not_retried(self):
        p = await self.check_failure(httpx.Response(200, json={'usage': {'prompt_tokens': 12, 'completion_tokens': 5}, 'choices': [{'message': {'content': 'invalid'}}]}))
        self.assertEqual(p.tokens_in, 12)
        self.assertEqual(p.tokens_out, 5)

    async def test_gateway_failure_is_not_retried(self):
        await self.check_failure(httpx.Response(504))

    async def test_truncated_response_is_not_retried(self):
        await self.check_failure(httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}))

    async def test_connect_timeout_retries(self):
        p = OpenAICompatProvider(Settings(llm_max_retries=3))
        await p._client.aclose()
        p._client = AsyncMock()
        p._client.post.side_effect = [httpx.ConnectTimeout(''), httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})]
        with patch('app.core.llm.openai_compat.asyncio.sleep', new_callable=AsyncMock):
            self.assertEqual(await p.complete_json('s', 'u'), {})
        self.assertEqual(p._client.post.call_count, 2)
