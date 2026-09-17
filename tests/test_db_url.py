"""连接串密码含特殊字符：应用自行编码，不要求 .env 里手工写 %40。"""

from sqlalchemy.engine import make_url

from app.db import normalize_db_url


def _parse(url: str):
    u = make_url(normalize_db_url(url))
    return u.username, u.password, u.host, u.port, u.database


def test_密码含_at_不再被当成主机名():
    assert _parse("mysql+pymysql://tiangong:Ab@@12@127.0.0.1:3306/tiangong?charset=utf8mb4") == (
        "tiangong", "Ab@@12", "127.0.0.1", 3306, "tiangong")


def test_密码含井号斜杠问号冒号百分号():
    assert _parse("mysql+pymysql://u:p#a/s?w:d%1@db.internal:3307/tg")[1] == "p#a/s?w:d%1"
    assert _parse("mysql+pymysql://u:p#a/s?w:d%1@db.internal:3307/tg")[2:] == ("db.internal", 3307, "tg")


def test_已编码的连接串等价不二次编码():
    raw = "mysql+pymysql://tiangong:Ab@@12@127.0.0.1:3306/tiangong"
    encoded = "mysql+pymysql://tiangong:Ab%40%4012@127.0.0.1:3306/tiangong"
    assert normalize_db_url(encoded) == normalize_db_url(raw) == encoded


def test_无密码与_sqlite_原样返回():
    assert normalize_db_url("mysql+pymysql://root@127.0.0.1:3306/tiangong") == "mysql+pymysql://root@127.0.0.1:3306/tiangong"
    assert normalize_db_url("sqlite:///./x.db") == "sqlite:///./x.db"
    assert normalize_db_url("sqlite://") == "sqlite://"
