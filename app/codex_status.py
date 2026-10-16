"""Recognize read-only progress questions without sending them to the worker."""
import re


def _pure_status(text):
    text = re.sub(r'\s+', '', text).strip('?？!！.。~,，')
    text = re.sub(r'^(?:코덱스(?:는|가|로|에게|한테)?|지금은?|현재|혹시|작업(?:은|이))+' , '', text)
    patterns = (
        r'(?:작업)?(?:진행상황|진행상태|상태|현황|진척상황|진척)(?:은|는|이|을)?(?:좀|한번|간단히)?(?:알려줘|알려주세요|알려줄래|보여줘|보여주세요|확인해줘|확인해주세요|보고해줘|보고해주세요|어때|어때요|어떤가요|어떻게돼|어떻게되나요|궁금해)?',
        r'(?:뭐|무엇을)(?:하고있어|하고있어요|하고있나요|하는중이야|하는중인가요|하고있는지알려줘)',
        r'어디까지(?:했어|했어요|됐어|됐어요|진행됐어|진행됐나요|진행했어|했나요|됐나요|했는지알려줘)',
        r'(?:다)?(?:끝났어|끝났어요|끝났나요|완료됐어|완료됐나요|됐어|됐나요|된거야|끝난거야)',
        r'(?:아직)?(?:작업중이야|진행중이야|실행중이야|돌아가고있어|돌아가고있나요|하고있어|하고있나요|어떻게되고있어|어떻게되고있나요)',
        r'(?:왜)?(?:멈췄어|멈췄나요|멈춘거야|답이없어|조용해)',
        r'하고있는(?:일|작업)(?:이)?(?:뭐야|무엇인가요)',
        r'(?:status|progress)',
    )
    return any(re.fullmatch(pattern,text,re.I) for pattern in patterns)


def status_query(text):
    if re.fullmatch(r'(?:\s*\d+[.)]\s*[^\n]+(?:\n|$))+',text):
        return None
    if _pure_status(text):
        return 'status'
    # A status question followed by another request must never become a decision answer.
    clauses = [part.strip() for part in re.split(r'[?？!！,，。\n]|\.(?=\s|$)|\s+(?:그리고|대신|그다음)\s+',text) if part.strip()]
    if len(clauses)>1 and any(_pure_status(part) for part in clauses):
        return 'mixed'
    return None
