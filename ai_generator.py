from google import genai

def create_ai_client(api_key):
    return genai.Client(api_key=api_key)

def generate_cover_letter(vacancy_text, config, ai_client):
    """Генерация сопроводительного письма через Gemini 2.0 Flash."""
    prompt = config["ai"]["prompt_template"].format(vacancy_text=vacancy_text)
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"Ошибка при генерации письма: {e}")
        return "Здравствуйте! Я изучил вашу вакансию и уверен, что мои навыки отлично подходят. Мое резюме прикреплено к сообщению. Буду рад обсудить детали."
