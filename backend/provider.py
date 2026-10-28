"""HKGAI / DeepSeek Chat Completions transport; no OpenAI service or SDK."""
import json
import threading
import urllib.error
import urllib.request


class ProviderError(RuntimeError):
    def __init__(self, message, code='provider', retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer credential to a different URL via redirects.
        return None


class ModelClient:
    def __init__(self, config):
        self.config = config
        self._slots = threading.BoundedSemaphore(3)

    def complete(self, messages, max_tokens=None):
        if not self.config.api_key:
            raise ProviderError('未配置 LLM_API_KEY。请在本机 .env 中填写课程 API key。')
        body = {'model': self.config.model, 'messages': messages, 'temperature': 0.2,
                'max_tokens': max_tokens or self.config.max_completion_tokens, 'stream': False}
        request = urllib.request.Request(
            self.config.base_url + '/chat/completions',
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.config.api_key},
            method='POST')
        try:
            with self._slots:
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.config.model_timeout) as response:
                    raw = response.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ProviderError('模型响应超过大小限制。')
                    result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get('choices'), list):
                raise ProviderError('模型 API 返回了无法识别的响应格式。')
            message = result['choices'][0]['message']
            if not isinstance(message, dict):
                raise ProviderError('模型 API 返回了无法识别的响应格式。')
            content = message.get('content')
            if not isinstance(content, str) or not content.strip():
                raise ProviderError('模型没有返回有效的文本响应。')
            return {'content': self.config.scrub(content), 'usage': self.config.scrub(result.get('usage', {})),
                    'model': result.get('model', self.config.model)}
        except urllib.error.HTTPError as error:
            # Do not echo provider response bodies; they may contain credentials or request content.
            raise ProviderError(f'模型 API 返回 HTTP {error.code}；请检查课程密钥、额度、模型和地址。',
                                'authorization' if error.code in (401,403) else 'provider_http',
                                error.code in (408,429,500,502,503,504)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderError(self.config.scrub('模型 API 连接失败：' + str(error)[:220]), 'network', True) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError('模型 API 返回了无法识别的响应格式。') from None
