import logging
from google import genai

log = logging.getLogger("vacancy_bot")

FALLBACK_COVER_LETTER = (
    "Здравствуйте! Я изучил вашу вакансию и уверен, что мои навыки отлично подходят. "
    "Мое резюме прикреплено к сообщению. Буду рад обсудить детали."
)

def create_ai_client(api_key, http_options=None):
    """http_options позволяет пустить запросы к Gemini через свой base_url
    (например, реверс-прокси на Cloudflare Worker) — см. network.mode в config.yaml."""
    if http_options is not None:
        return genai.Client(api_key=api_key, http_options=http_options)
    return genai.Client(api_key=api_key)

def generate_cover_letter(vacancy_text, config, ai_client):
    """Генерация сопроводительного письма через Gemini 2.0 Flash."""
    prompt = config["ai"]["prompt_template"].format(vacancy_text=vacancy_text)

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        log.error(f"Ошибка при генерации письма: {e}")
        return FALLBACK_COVER_LETTER
