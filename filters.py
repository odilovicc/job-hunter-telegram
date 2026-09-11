import re

def level_1_filter(text, config):
    """Быстрый фильтр по ключевым словам (Regex)."""
    text_lower = text.lower()
    
    # Проверка на стоп-слова (СНАЧАЛА)
    excludes = config["filters"]["level_1_keywords"]["exclude"]
    for word in excludes:
        if re.search(r'(?<!\w)' + re.escape(word.lower()) + r'(?!\w)', text_lower):
            return False

    # Проверка на ключевые слова
    includes = config["filters"]["level_1_keywords"]["include"]
    for word in includes:
        if re.search(r'(?<!\w)' + re.escape(word.lower()) + r'(?!\w)', text_lower):
            return True
            
    return False

def level_2_scoring(text, config):
    """Алгоритмическая оценка вакансии. Возвращает (score, passed, breakdown)."""
    text_lower = text.lower()
    score = 0
    breakdown = []  # #14: Расшифровка баллов
    
    rules = config["filters"]["level_2_scoring"]["rules"]
    min_score = config["filters"]["level_2_scoring"]["min_pass_score"]
    
    for rule in rules:
        pattern = rule["pattern"]
        if re.search(pattern, text_lower):
            points = rule["score"]
            score += points
            label = rule.get("label", pattern)
            if points > 0:
                breakdown.append(f"{label} (+{points})")
            
    return score, score >= min_score, breakdown
