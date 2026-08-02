/*
 * parser_toml.c — TOML v1.0 常用子集解析器。
 *
 * 支持：基本/字面字符串（含三引号多行）、整数（dec/hex/oct/bin + 下划线）、
 *       浮点（含 inf/nan）、布尔、数组（含尾随逗号/多行）、内联表、
 *       [a.b] 表、[[a.b]] 表数组（-> list[dict]）、点分键、
 *       datetime -> ISO-8601 字符串。
 * 行为：重复键 / 重复表头报错（对齐 TOML 规范）。
 * 错误：统一 ValueError("parse error at line L, column C: <msg>")。
 */

#include <Python.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern void cfgdrift_raise_error(const char* msg, int line, int col);
extern void cfgdrift_line_col(const char* text, Py_ssize_t offset,
                              int* line, int* col);
extern PyObject* cfgdrift_new_str(const char* s, Py_ssize_t len);

/* ------------------------------------------------------------------ */
/* 上下文                                                               */
/* ------------------------------------------------------------------ */

#define TOML_MAX_DEPTH 512

typedef struct {
    const char* text;
    Py_ssize_t len;
    Py_ssize_t pos;
    int depth;
    PyObject* root;                /* 顶层 dict */
    PyObject* current_table;       /* 当前表容器（引用持有） */
    PyObject* current_table_path;  /* 当前表路径（PyList of str，根=空表） */
    PyObject* current_table_path_flags; /* 当前表路径各段是否被引号包裹 */
    PyObject* defined_keys;        /* set of absolute dotted paths (str) */
    PyObject* defined_tables;      /* set of table paths (str) */
    PyObject* defined_arrays;      /* set of array-table paths (str) */
    PyObject* implicit_tables;     /* set of table paths created implicitly */
} TomlCtx;

static void toml_error(TomlCtx* ctx, const char* msg)
{
    int line, col;
    cfgdrift_line_col(ctx->text, ctx->pos, &line, &col);
    cfgdrift_raise_error(msg, line, col);
}

static int toml_is_ws(char c)
{
    return c == ' ' || c == '\t';
}

static int toml_is_newline(char c)
{
    return c == '\n' || c == '\r';
}

static void toml_skip_ws(TomlCtx* ctx)
{
    while (ctx->pos < ctx->len &&
           (toml_is_ws(ctx->text[ctx->pos]) ||
            toml_is_newline(ctx->text[ctx->pos]))) {
        ctx->pos++;
    }
}

/* 跳过空白与注释（注释到行尾）。 */
static void toml_skip_ws_comments(TomlCtx* ctx)
{
    for (;;) {
        while (ctx->pos < ctx->len &&
               (toml_is_ws(ctx->text[ctx->pos]) ||
                toml_is_newline(ctx->text[ctx->pos]))) {
            ctx->pos++;
        }
        if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '#') {
            while (ctx->pos < ctx->len && !toml_is_newline(ctx->text[ctx->pos])) {
                ctx->pos++;
            }
            continue;
        }
        break;
    }
}

/* ------------------------------------------------------------------ */
/* 字符串                                                               */
/* ------------------------------------------------------------------ */

typedef struct {
    char* buf;
    Py_ssize_t cap;
    Py_ssize_t len;
} StrBuf;

static int sb_init(StrBuf* sb)
{
    sb->cap = 128;
    sb->len = 0;
    sb->buf = (char*)malloc((size_t)sb->cap);
    return sb->buf != NULL;
}

static int sb_append(StrBuf* sb, const char* data, Py_ssize_t n)
{
    if (sb->len + n > sb->cap) {
        while (sb->len + n > sb->cap) {
            sb->cap *= 2;
        }
        {
            char* nb = (char*)realloc(sb->buf, (size_t)sb->cap);
            if (nb == NULL) {
                return -1;
            }
            sb->buf = nb;
        }
    }
    memcpy(sb->buf + sb->len, data, (size_t)n);
    sb->len += n;
    return 0;
}

static void sb_free(StrBuf* sb)
{
    free(sb->buf);
    sb->buf = NULL;
    sb->len = 0;
    sb->cap = 0;
}

static int toml_hex_digit(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int toml_append_utf8(StrBuf* sb, unsigned int cp)
{
    unsigned char utf8[4];
    int n;
    if (cp < 0x80) {
        utf8[0] = (unsigned char)cp; n = 1;
    } else if (cp < 0x800) {
        utf8[0] = (unsigned char)(0xC0 | (cp >> 6));
        utf8[1] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 2;
    } else if (cp < 0x10000) {
        utf8[0] = (unsigned char)(0xE0 | (cp >> 12));
        utf8[1] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        utf8[2] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 3;
    } else {
        utf8[0] = (unsigned char)(0xF0 | (cp >> 18));
        utf8[1] = (unsigned char)(0x80 | ((cp >> 12) & 0x3F));
        utf8[2] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        utf8[3] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 4;
    }
    return sb_append(sb, (const char*)utf8, n);
}

static int toml_parse_escape(TomlCtx* ctx, StrBuf* sb)
{
    char c;
    if (ctx->pos >= ctx->len) {
        toml_error(ctx, "unterminated escape sequence");
        return -1;
    }
    c = ctx->text[ctx->pos];
    ctx->pos++;
    switch (c) {
    case 'b': return sb_append(sb, "\b", 1);
    case 't': return sb_append(sb, "\t", 1);
    case 'n': return sb_append(sb, "\n", 1);
    case 'f': return sb_append(sb, "\f", 1);
    case 'r': return sb_append(sb, "\r", 1);
    case '"': return sb_append(sb, "\"", 1);
    case '\\': return sb_append(sb, "\\", 1);
    case 'u':
    case 'U': {
        int digits = (c == 'u') ? 4 : 8;
        unsigned int cp = 0;
        int i;
        if (ctx->pos + digits > ctx->len) {
            toml_error(ctx, "invalid unicode escape");
            return -1;
        }
        for (i = 0; i < digits; i++) {
            int d = toml_hex_digit(ctx->text[ctx->pos + i]);
            if (d < 0) {
                toml_error(ctx, "invalid unicode escape");
                return -1;
            }
            cp = (cp << 4) | (unsigned int)d;
        }
        ctx->pos += digits;
        if (cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
            toml_error(ctx, "invalid unicode scalar value");
            return -1;
        }
        return toml_append_utf8(sb, cp);
    }
    default:
        toml_error(ctx, "invalid escape character");
        return -1;
    }
}

/* 基本字符串（当前位于开引号 '"' 之后）。 */
static PyObject* toml_parse_basic_string(TomlCtx* ctx, int triple)
{
    StrBuf sb;
    int closed = 0;

    if (!sb_init(&sb)) {
        PyErr_NoMemory();
        return NULL;
    }

    while (ctx->pos < ctx->len) {
        char c = ctx->text[ctx->pos];
        if (c == '"') {
            if (triple && ctx->pos + 2 < ctx->len &&
                ctx->text[ctx->pos + 1] == '"' &&
                ctx->text[ctx->pos + 2] == '"') {
                ctx->pos += 3;
                closed = 1;
                break;
            }
            if (!triple) {
                ctx->pos++;
                closed = 1;
                break;
            }
            if (sb_append(&sb, "\"", 1) < 0) {
                PyErr_NoMemory();
                sb_free(&sb);
                return NULL;
            }
            ctx->pos++;
            continue;
        }
        if (c == '\\') {
            ctx->pos++;
            if (triple && ctx->pos < ctx->len &&
                ctx->text[ctx->pos] == '\n') {
                ctx->pos++;
                while (ctx->pos < ctx->len &&
                       (toml_is_ws(ctx->text[ctx->pos]) ||
                        toml_is_newline(ctx->text[ctx->pos]))) {
                    ctx->pos++;
                }
                continue;
            }
            if (triple && ctx->pos < ctx->len &&
                ctx->text[ctx->pos] == '\r') {
                ctx->pos++;
                if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '\n') {
                    ctx->pos++;
                }
                while (ctx->pos < ctx->len &&
                       (toml_is_ws(ctx->text[ctx->pos]) ||
                        toml_is_newline(ctx->text[ctx->pos]))) {
                    ctx->pos++;
                }
                continue;
            }
            if (toml_parse_escape(ctx, &sb) < 0) {
                sb_free(&sb);
                return NULL;
            }
            continue;
        }
        if (!triple && (c == '\n' || c == '\r')) {
            toml_error(ctx, "newline in single-line string");
            sb_free(&sb);
            return NULL;
        }
        if ((unsigned char)c < 0x20 && c != '\t' && c != '\n' && c != '\r') {
            toml_error(ctx, "control character in string");
            sb_free(&sb);
            return NULL;
        }
        if (sb_append(&sb, &c, 1) < 0) {
            PyErr_NoMemory();
            sb_free(&sb);
            return NULL;
        }
        ctx->pos++;
    }

    if (!closed) {
        toml_error(ctx, "unterminated string");
        sb_free(&sb);
        return NULL;
    }
    if (triple && sb.len > 0 && sb.buf[0] == '\n') {
        memmove(sb.buf, sb.buf + 1, (size_t)(sb.len - 1));
        sb.len--;
    } else if (triple && sb.len > 0 && sb.buf[0] == '\r' &&
               sb.len > 1 && sb.buf[1] == '\n') {
        memmove(sb.buf, sb.buf + 2, (size_t)(sb.len - 2));
        sb.len -= 2;
    }
    {
        PyObject* result = cfgdrift_new_str(sb.buf, sb.len);
        sb_free(&sb);
        return result;
    }
}

/* 字面字符串（当前位于开引号 '\'' 之后）。 */
static PyObject* toml_parse_literal_string(TomlCtx* ctx, int triple)
{
    StrBuf sb;
    int closed = 0;

    if (!sb_init(&sb)) {
        PyErr_NoMemory();
        return NULL;
    }

    while (ctx->pos < ctx->len) {
        char c = ctx->text[ctx->pos];
        if (c == '\'') {
            if (triple && ctx->pos + 2 < ctx->len &&
                ctx->text[ctx->pos + 1] == '\'' &&
                ctx->text[ctx->pos + 2] == '\'') {
                ctx->pos += 3;
                closed = 1;
                break;
            }
            if (!triple) {
                ctx->pos++;
                closed = 1;
                break;
            }
            if (sb_append(&sb, "'", 1) < 0) {
                PyErr_NoMemory();
                sb_free(&sb);
                return NULL;
            }
            ctx->pos++;
            continue;
        }
        if (!triple && (c == '\n' || c == '\r')) {
            toml_error(ctx, "newline in single-line literal string");
            sb_free(&sb);
            return NULL;
        }
        if (sb_append(&sb, &c, 1) < 0) {
            PyErr_NoMemory();
            sb_free(&sb);
            return NULL;
        }
        ctx->pos++;
    }

    if (!closed) {
        toml_error(ctx, "unterminated literal string");
        sb_free(&sb);
        return NULL;
    }
    if (triple && sb.len > 0 && sb.buf[0] == '\n') {
        memmove(sb.buf, sb.buf + 1, (size_t)(sb.len - 1));
        sb.len--;
    } else if (triple && sb.len > 0 && sb.buf[0] == '\r' &&
               sb.len > 1 && sb.buf[1] == '\n') {
        memmove(sb.buf, sb.buf + 2, (size_t)(sb.len - 2));
        sb.len -= 2;
    }
    {
        PyObject* result = cfgdrift_new_str(sb.buf, sb.len);
        sb_free(&sb);
        return result;
    }
}

/* 键名：裸键 / 基本字符串键 / 字面字符串键。返回新引用 PyUnicode。 */
static PyObject* toml_parse_key(TomlCtx* ctx)
{
    char c;
    if (ctx->pos >= ctx->len) {
        toml_error(ctx, "unexpected end of input in key");
        return NULL;
    }
    c = ctx->text[ctx->pos];
    if (c == '"') {
        ctx->pos++;
        return toml_parse_basic_string(ctx, 0);
    }
    if (c == '\'') {
        ctx->pos++;
        return toml_parse_literal_string(ctx, 0);
    }
    {
        Py_ssize_t start = ctx->pos;
        while (ctx->pos < ctx->len) {
            char k = ctx->text[ctx->pos];
            if ((k >= 'A' && k <= 'Z') || (k >= 'a' && k <= 'z') ||
                (k >= '0' && k <= '9') || k == '_' || k == '-') {
                ctx->pos++;
            } else {
                break;
            }
        }
        if (ctx->pos == start) {
            toml_error(ctx, "invalid key");
            return NULL;
        }
        return cfgdrift_new_str(ctx->text + start, ctx->pos - start);
    }
}

/* ------------------------------------------------------------------ */
/* 数字 / 日期时间                                                       */
/* ------------------------------------------------------------------ */

static int toml_is_token_char(char c)
{
    if (toml_is_ws(c) || toml_is_newline(c)) return 0;
    if (c == ',' || c == ']' || c == '}' || c == '#' || c == '=') return 0;
    return 1;
}

static int toml_token_is_datetime(const char* s, Py_ssize_t n)
{
    int i;
    int has_colon = 0;
    for (i = 0; i < (int)n; i++) {
        if (s[i] == ':') return 1;
    }
    if (n >= 10 && s[0] >= '0' && s[0] <= '9' && s[4] == '-' && s[7] == '-') {
        return 1;
    }
    return 0;
}

/* TOML v1.0 整数数字字符判定（含各进制）。 */
static int toml_int_is_digit_char(char c, int base)
{
    if (base == 16) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
               (c >= 'A' && c <= 'F');
    }
    if (base == 8) {
        return c >= '0' && c <= '7';
    }
    if (base == 2) {
        return c == '0' || c == '1';
    }
    return c >= '0' && c <= '9';
}

/* 解析整数 token（可能带 +/- 前缀与 0x/0o/0b 基前缀）。
 * 任意精度：直接用 PyLong_FromString 构造 PyLong（不会溢出/截断），
 * 缓冲区按 token 长度动态分配，杜绝栈缓冲区溢出。
 *
 * TOML v1.0 语法校验（对齐 tomllib 的规范行为）：
 *  ① 非十进制（0x/0o/0b）禁止 +/- 符号；
 *  ② 十进制禁止前导零（单个 0 或 ±0 除外）；
 *  ③ 下划线必须夹在数字之间（禁止进制前缀后 / 连续 / 尾随）。 */
static PyObject* toml_parse_int_token(TomlCtx* ctx, const char* s, Py_ssize_t n)
{
    Py_ssize_t i = 0;
    int base = 10;
    int has_sign = 0;
    Py_ssize_t digit_start, digit_end, k;
    char* tmp;
    Py_ssize_t j = 0;
    PyObject* result;

    /* 基前缀识别（符号位单独保留）。 */
    has_sign = (n > 0 && (s[0] == '+' || s[0] == '-')) ? 1 : 0;
    i = has_sign ? 1 : 0;
    if (i + 1 < n && s[i] == '0' && (s[i + 1] == 'x' || s[i + 1] == 'X')) {
        base = 16; i += 2;
    } else if (i + 1 < n && s[i] == '0' && (s[i + 1] == 'o' || s[i + 1] == 'O')) {
        base = 8; i += 2;
    } else if (i + 1 < n && s[i] == '0' && (s[i + 1] == 'b' || s[i + 1] == 'B')) {
        base = 2; i += 2;
    }

    /* ① 非十进制禁止符号。 */
    if (base != 10 && has_sign) {
        toml_error(ctx, "sign not allowed on non-decimal integer");
        return NULL;
    }
    /* ② 十进制禁止前导零。 */
    digit_start = i;
    digit_end = n;
    if (base == 10 && digit_end - digit_start > 1 && s[digit_start] == '0') {
        toml_error(ctx, "leading zeros are not allowed in integers");
        return NULL;
    }
    /* ③ 字符合法性 + 下划线必须夹在数字之间。 */
    for (k = digit_start; k < digit_end; k++) {
        char c = s[k];
        if (c == '_') {
            if (k == digit_start || k == digit_end - 1 ||
                !toml_int_is_digit_char(s[k - 1], base) ||
                !toml_int_is_digit_char(s[k + 1], base)) {
                toml_error(ctx, "underscores must be surrounded by digits");
                return NULL;
            }
        } else if (!toml_int_is_digit_char(c, base)) {
            toml_error(ctx, "invalid digit in integer");
            return NULL;
        }
    }

    tmp = (char*)malloc((size_t)n + 1);
    if (tmp == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    /* 先拷贝符号（若有），再拷贝去下划线后的数字。 */
    if (has_sign) {
        tmp[j++] = s[0];
    }
    for (; i < n; i++) {
        char c = s[i];
        if (c != '_') {
            tmp[j++] = c;
        }
    }
    if (j == 0 || (j == 1 && (tmp[0] == '+' || tmp[0] == '-'))) {
        toml_error(ctx, "invalid integer");
        free(tmp);
        return NULL;
    }
    tmp[j] = '\0';
    result = PyLong_FromString(tmp, NULL, base);
    free(tmp);
    if (result == NULL) {
        /* PyLong_FromString 只会在非法字符时报错（已前置校验），
         * 这里兜底清除并抛出统一格式错误。 */
        PyErr_Clear();
        toml_error(ctx, "invalid integer");
        return NULL;
    }
    return result;
}

static PyObject* toml_parse_float_token(TomlCtx* ctx, const char* s, Py_ssize_t n)
{
    char tmp[96];
    Py_ssize_t i, j = 0;
    double value;

    if (n == 3 && memcmp(s, "inf", 3) == 0) return PyFloat_FromDouble(INFINITY);
    if (n == 4 && memcmp(s, "+inf", 4) == 0) return PyFloat_FromDouble(INFINITY);
    if (n == 4 && memcmp(s, "-inf", 4) == 0) return PyFloat_FromDouble(-INFINITY);
    if (n == 3 && memcmp(s, "nan", 3) == 0) return PyFloat_FromDouble(NAN);
    if (n == 4 && memcmp(s, "+nan", 4) == 0) return PyFloat_FromDouble(NAN);
    if (n == 4 && memcmp(s, "-nan", 4) == 0) return PyFloat_FromDouble(NAN);
    for (i = 0; i < n && j < 95; i++) {
        if (s[i] != '_') tmp[j++] = s[i];
    }
    tmp[j] = '\0';
    value = strtod(tmp, NULL);
    return PyFloat_FromDouble(value);
}

/* ------------------------------------------------------------------ */
/* 值解析（前置：已跳过空白）                                           */
/* ------------------------------------------------------------------ */

static PyObject* toml_parse_value(TomlCtx* ctx);

static PyObject* toml_parse_array(TomlCtx* ctx)
{
    PyObject* list = PyList_New(0);
    if (list == NULL) return NULL;

    ctx->pos++;
    toml_skip_ws_comments(ctx);
    if (ctx->pos < ctx->len && ctx->text[ctx->pos] == ']') {
        ctx->pos++;
        return list;
    }
    for (;;) {
        PyObject* item;
        toml_skip_ws_comments(ctx);
        item = toml_parse_value(ctx);
        if (item == NULL) {
            Py_DECREF(list);
            return NULL;
        }
        if (PyList_Append(list, item) < 0) {
            Py_DECREF(item);
            Py_DECREF(list);
            return NULL;
        }
        Py_DECREF(item);
        toml_skip_ws_comments(ctx);
        if (ctx->pos >= ctx->len) {
            toml_error(ctx, "unterminated array");
            Py_DECREF(list);
            return NULL;
        }
        if (ctx->text[ctx->pos] == ',') {
            ctx->pos++;
            toml_skip_ws_comments(ctx);
            if (ctx->pos < ctx->len && ctx->text[ctx->pos] == ']') {
                ctx->pos++;
                return list;
            }
            continue;
        }
        if (ctx->text[ctx->pos] == ']') {
            ctx->pos++;
            return list;
        }
        toml_error(ctx, "expected ',' or ']' in array");
        Py_DECREF(list);
        return NULL;
    }
}

static PyObject* toml_parse_inline_table(TomlCtx* ctx)
{
    PyObject* dict = PyDict_New();
    if (dict == NULL) return NULL;

    ctx->pos++;
    toml_skip_ws(ctx);
    if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '}') {
        ctx->pos++;
        return dict;
    }
    for (;;) {
        PyObject* key;
        PyObject* value;
        toml_skip_ws(ctx);
        key = toml_parse_key(ctx);
        if (key == NULL) {
            Py_DECREF(dict);
            return NULL;
        }
        toml_skip_ws(ctx);
        if (ctx->pos >= ctx->len || ctx->text[ctx->pos] != '=') {
            Py_DECREF(key);
            Py_DECREF(dict);
            toml_error(ctx, "expected '=' in inline table");
            return NULL;
        }
        ctx->pos++;
        toml_skip_ws(ctx);
        value = toml_parse_value(ctx);
        if (value == NULL) {
            Py_DECREF(key);
            Py_DECREF(dict);
            return NULL;
        }
        if (PyDict_SetItem(dict, key, value) < 0) {
            Py_DECREF(key);
            Py_DECREF(value);
            Py_DECREF(dict);
            return NULL;
        }
        Py_DECREF(key);
        Py_DECREF(value);
        toml_skip_ws(ctx);
        if (ctx->pos >= ctx->len) {
            toml_error(ctx, "unterminated inline table");
            Py_DECREF(dict);
            return NULL;
        }
        if (ctx->text[ctx->pos] == ',') {
            ctx->pos++;
            toml_skip_ws(ctx);
            if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '}') {
                toml_error(ctx, "trailing comma in inline table");
                Py_DECREF(dict);
                return NULL;
            }
            continue;
        }
        if (ctx->text[ctx->pos] == '}') {
            ctx->pos++;
            return dict;
        }
        toml_error(ctx, "expected ',' or '}' in inline table");
        Py_DECREF(dict);
        return NULL;
    }
}

static PyObject* toml_parse_value(TomlCtx* ctx)
{
    char c;
    PyObject* result = NULL;
    toml_skip_ws_comments(ctx);
    if (ctx->pos >= ctx->len) {
        toml_error(ctx, "unexpected end of input in value");
        return NULL;
    }
    if (ctx->depth >= TOML_MAX_DEPTH) {
        toml_error(ctx, "max depth exceeded");
        return NULL;
    }
    ctx->depth++;
    c = ctx->text[ctx->pos];

    if (c == '"') {
        int triple = (ctx->pos + 2 < ctx->len &&
                      ctx->text[ctx->pos + 1] == '"' &&
                      ctx->text[ctx->pos + 2] == '"');
        ctx->pos += triple ? 3 : 1;
        result = toml_parse_basic_string(ctx, triple);
    } else if (c == '\'') {
        int triple = (ctx->pos + 2 < ctx->len &&
                      ctx->text[ctx->pos + 1] == '\'' &&
                      ctx->text[ctx->pos + 2] == '\'');
        ctx->pos += triple ? 3 : 1;
        result = toml_parse_literal_string(ctx, triple);
    } else if (c == '[') {
        result = toml_parse_array(ctx);
    } else if (c == '{') {
        result = toml_parse_inline_table(ctx);
    } else if (c == 't' && ctx->len - ctx->pos >= 4 &&
               memcmp(ctx->text + ctx->pos, "true", 4) == 0) {
        ctx->pos += 4;
        Py_INCREF(Py_True);
        result = Py_True;
    } else if (c == 'f' && ctx->len - ctx->pos >= 5 &&
               memcmp(ctx->text + ctx->pos, "false", 5) == 0) {
        ctx->pos += 5;
        Py_INCREF(Py_False);
        result = Py_False;
    } else if ((c >= '0' && c <= '9') || c == '+' || c == '-' || c == 'i' || c == 'n') {
        Py_ssize_t start = ctx->pos;
        while (ctx->pos < ctx->len && toml_is_token_char(ctx->text[ctx->pos])) {
            ctx->pos++;
        }
        {
            const char* tok = ctx->text + start;
            Py_ssize_t n = ctx->pos - start;
            int has_dot = 0, has_e = 0, is_hex = 0, i;
            int start_i = (n > 0 && (tok[0] == '+' || tok[0] == '-')) ? 1 : 0;
            /* Hex integers may legitimately contain 'e'/'E' (e.g. 0xDEADBEEF);
             * only treat e/E as a float exponent outside hex literals. */
            if (n - start_i >= 2 && tok[start_i] == '0' &&
                (tok[start_i + 1] == 'x' || tok[start_i + 1] == 'X')) {
                is_hex = 1;
            }
            for (i = 0; i < (int)n; i++) {
                if (tok[i] == '.') has_dot = 1;
                if ((tok[i] == 'e' || tok[i] == 'E') && !is_hex) has_e = 1;
            }
            if (toml_token_is_datetime(tok, n)) {
                result = cfgdrift_new_str(tok, n);
            } else if (has_dot || has_e ||
                (n == 3 && memcmp(tok, "inf", 3) == 0) ||
                (n == 4 && memcmp(tok, "+inf", 4) == 0) ||
                (n == 4 && memcmp(tok, "-inf", 4) == 0) ||
                (n == 3 && memcmp(tok, "nan", 3) == 0) ||
                (n == 4 && memcmp(tok, "+nan", 4) == 0) ||
                (n == 4 && memcmp(tok, "-nan", 4) == 0)) {
                result = toml_parse_float_token(ctx, tok, n);
            } else {
                result = toml_parse_int_token(ctx, tok, n);
            }
        }
    } else {
        toml_error(ctx, "unexpected character in value");
    }
    ctx->depth--;
    return result;
}

/* ------------------------------------------------------------------ */
/* 键路径 / 表导航                                                       */
/* ------------------------------------------------------------------ */

/* 点分键序列（到行尾或 '=' 之前）。
 * 返回 PyList（PyUnicode 元素，原始键名）；*flags_out 返回并行 PyList
 * （0=裸键，1=被引号包裹），用于构造唯一规范路径。 */
static PyObject* toml_parse_key_path(TomlCtx* ctx, int stop_at_eq,
                                     PyObject** flags_out)
{
    PyObject* parts = PyList_New(0);
    PyObject* flags = PyList_New(0);
    if (parts == NULL || flags == NULL) {
        Py_XDECREF(parts);
        Py_XDECREF(flags);
        return NULL;
    }
    *flags_out = flags;

    for (;;) {
        PyObject* key;
        toml_skip_ws(ctx);
        if (ctx->pos >= ctx->len ||
            (stop_at_eq && ctx->text[ctx->pos] == '=')) {
            if (PyList_GET_SIZE(parts) == 0) {
                Py_DECREF(parts);
                Py_DECREF(flags);
                toml_error(ctx, "expected key");
                return NULL;
            }
            return parts;
        }
        {
            char fc = ctx->text[ctx->pos];
            PyObject* flag = (fc == '"' || fc == '\'') ? Py_True : Py_False;
            if (PyList_Append(flags, flag) < 0) {
                Py_DECREF(parts);
                Py_DECREF(flags);
                return NULL;
            }
        }
        key = toml_parse_key(ctx);
        if (key == NULL) {
            Py_DECREF(parts);
            Py_DECREF(flags);
            return NULL;
        }
        if (PyList_Append(parts, key) < 0) {
            Py_DECREF(key);
            Py_DECREF(parts);
            Py_DECREF(flags);
            return NULL;
        }
        Py_DECREF(key);
        toml_skip_ws(ctx);
        if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '.') {
            ctx->pos++;
            continue;
        }
        return parts;
    }
}

/* 转义单个路径段（引号包裹的段转义 \\ 与 .，裸键无需转义）。
 * 返回新分配 C 字符串。 */
static char* toml_escape_segment(const char* s, Py_ssize_t n, int quoted)
{
    size_t cap = (size_t)n + 1;
    char* out;
    Py_ssize_t i;
    size_t j = 0;
    if (!quoted) {
        out = (char*)malloc(cap);
        if (out == NULL) return NULL;
        memcpy(out, s, (size_t)n);
        out[n] = '\0';
        return out;
    }
    for (i = 0; i < n; i++) {
        if (s[i] == '\\' || s[i] == '.') {
            cap += 1;
        }
    }
    out = (char*)malloc(cap);
    if (out == NULL) return NULL;
    for (i = 0; i < n; i++) {
        if (s[i] == '\\' || s[i] == '.') {
            out[j++] = '\\';
        }
        out[j++] = s[i];
    }
    out[j] = '\0';
    return out;
}

static int toml_segment_quoted(PyObject* flags, Py_ssize_t i)
{
    if (flags == NULL || i >= PyList_GET_SIZE(flags)) {
        return 0;
    }
    return PyObject_IsTrue(PyList_GET_ITEM(flags, i)) == 1;
}

/* 将 PyList（str 元素）+ 引号标志连接成规范路径 "a.b.c"。
 * 引号包裹的段转义 '.'（如 "a.b" -> "a\\.b"），从而与点分键区分。
 * 返回新分配 C 字符串。 */
static char* toml_path_str(PyObject* parts, PyObject* flags)
{
    Py_ssize_t n = PyList_GET_SIZE(parts);
    Py_ssize_t i;
    size_t total = 1;
    for (i = 0; i < n; i++) {
        Py_ssize_t klen;
        const char* ks = PyUnicode_AsUTF8AndSize(PyList_GET_ITEM(parts, i), &klen);
        Py_ssize_t k;
        if (ks == NULL) return NULL;
        total += (size_t)klen + 1;
        if (toml_segment_quoted(flags, i)) {
            for (k = 0; k < klen; k++) {
                if (ks[k] == '\\' || ks[k] == '.') total += 1;
            }
        }
    }
    {
        char* out = (char*)malloc(total);
        size_t pos = 0;
        if (out == NULL) return NULL;
        for (i = 0; i < n; i++) {
            Py_ssize_t klen;
            const char* ks = PyUnicode_AsUTF8AndSize(PyList_GET_ITEM(parts, i), &klen);
            char* esc;
            if (ks == NULL) {
                free(out);
                return NULL;
            }
            if (i > 0) out[pos++] = '.';
            esc = toml_escape_segment(ks, klen, toml_segment_quoted(flags, i));
            if (esc == NULL) {
                free(out);
                PyErr_NoMemory();
                return NULL;
            }
            memcpy(out + pos, esc, strlen(esc));
            pos += strlen(esc);
            free(esc);
        }
        out[pos] = '\0';
        return out;
    }
}

/* 绝对路径 = current_table_path（含其引号标志）+ parts（含引号标志）。 */
static char* toml_abs_path_str(TomlCtx* ctx, PyObject* parts, PyObject* flags)
{
    PyObject* cur = ctx->current_table_path;
    PyObject* cur_flags = ctx->current_table_path_flags;
    Py_ssize_t cn = PyList_GET_SIZE(cur);
    Py_ssize_t pn = PyList_GET_SIZE(parts);
    size_t total = 1;
    Py_ssize_t i;
    for (i = 0; i < cn; i++) {
        Py_ssize_t klen;
        const char* ks = PyUnicode_AsUTF8AndSize(PyList_GET_ITEM(cur, i), &klen);
        Py_ssize_t k;
        if (ks == NULL) return NULL;
        total += (size_t)klen + 1;
        if (toml_segment_quoted(cur_flags, i)) {
            for (k = 0; k < klen; k++) {
                if (ks[k] == '\\' || ks[k] == '.') total += 1;
            }
        }
    }
    for (i = 0; i < pn; i++) {
        Py_ssize_t klen;
        const char* ks = PyUnicode_AsUTF8AndSize(PyList_GET_ITEM(parts, i), &klen);
        Py_ssize_t k;
        if (ks == NULL) return NULL;
        total += (size_t)klen + 1;
        if (toml_segment_quoted(flags, i)) {
            for (k = 0; k < klen; k++) {
                if (ks[k] == '\\' || ks[k] == '.') total += 1;
            }
        }
    }
    {
        char* out = (char*)malloc(total);
        size_t pos = 0;
        int first = 1;
        if (out == NULL) return NULL;
        for (i = 0; i < cn; i++) {
            Py_ssize_t klen;
            const char* ks = PyUnicode_AsUTF8AndSize(PyList_GET_ITEM(cur, i), &klen);
            char* esc;
            if (ks == NULL) {
                free(out);
                return NULL;
            }
            if (!first) out[pos++] = '.';
            first = 0;
            esc = toml_escape_segment(ks, klen, toml_segment_quoted(cur_flags, i));
            if (esc == NULL) {
                free(out);
                PyErr_NoMemory();
                return NULL;
            }
            memcpy(out + pos, esc, strlen(esc));
            pos += strlen(esc);
            free(esc);
        }
        for (i = 0; i < pn; i++) {
            Py_ssize_t klen;
            const char* ks = PyUnicode_AsUTF8AndSize(PyList_GET_ITEM(parts, i), &klen);
            char* esc;
            if (ks == NULL) {
                free(out);
                return NULL;
            }
            if (!first) out[pos++] = '.';
            first = 0;
            esc = toml_escape_segment(ks, klen, toml_segment_quoted(flags, i));
            if (esc == NULL) {
                free(out);
                PyErr_NoMemory();
                return NULL;
            }
            memcpy(out + pos, esc, strlen(esc));
            pos += strlen(esc);
            free(esc);
        }
        out[pos] = '\0';
        return out;
    }
}

static int toml_set_add_str(PyObject* set, const char* path)
{
    PyObject* s = cfgdrift_new_str(path, strlen(path));
    int rc;
    if (s == NULL) return -1;
    rc = PySet_Add(set, s);
    Py_DECREF(s);
    return rc;
}

static int toml_set_contains_str(PyObject* set, const char* path)
{
    PyObject* s = cfgdrift_new_str(path, strlen(path));
    int rc;
    if (s == NULL) return -1;
    rc = PySet_Contains(set, s);
    Py_DECREF(s);
    return rc;
}

/* 沿 parts 在 container 下导航（必要时创建中间 dict）。 */
static PyObject* toml_navigate(TomlCtx* ctx, PyObject* container, PyObject* parts,
                               int create_missing)
{
    Py_ssize_t n = PyList_GET_SIZE(parts);
    PyObject* cur = container;
    Py_ssize_t i;
    for (i = 0; i < n - 1; i++) {
        PyObject* key = PyList_GET_ITEM(parts, i);
        PyObject* child = PyDict_GetItemWithError(cur, key);
        if (child == NULL) {
            if (PyErr_Occurred()) return NULL;
            if (!create_missing) {
                toml_error(ctx, "table does not exist");
                return NULL;
            }
            child = PyDict_New();
            if (child == NULL) return NULL;
            if (PyDict_SetItem(cur, key, child) < 0) {
                Py_DECREF(child);
                return NULL;
            }
            Py_DECREF(child);
        } else if (!PyDict_Check(child)) {
            toml_error(ctx, "cannot extend a key as a table");
            return NULL;
        }
        cur = child;
    }
    return cur;
}

/* 记录中间前缀为隐式表。引号段按规范路径转义。 */
static int toml_mark_implicit(TomlCtx* ctx, PyObject* parts, PyObject* flags)
{
    Py_ssize_t n = PyList_GET_SIZE(parts);
    Py_ssize_t i;
    for (i = 0; i < n - 1; i++) {
        Py_ssize_t j;
        size_t total = 1;
        char* prefix;
        for (j = 0; j <= i; j++) {
            Py_ssize_t kl2;
            const char* ks2 = PyUnicode_AsUTF8AndSize(PyList_GET_ITEM(parts, j), &kl2);
            Py_ssize_t k;
            if (ks2 == NULL) return -1;
            total += (size_t)kl2 + 1;
            if (toml_segment_quoted(flags, j)) {
                for (k = 0; k < kl2; k++) {
                    if (ks2[k] == '\\' || ks2[k] == '.') total += 1;
                }
            }
        }
        prefix = (char*)malloc(total);
        if (prefix == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        prefix[0] = '\0';
        for (j = 0; j <= i; j++) {
            Py_ssize_t kl2;
            const char* ks2 = PyUnicode_AsUTF8AndSize(PyList_GET_ITEM(parts, j), &kl2);
            char* esc;
            if (ks2 == NULL) {
                free(prefix);
                return -1;
            }
            if (j > 0) strcat(prefix, ".");
            esc = toml_escape_segment(ks2, kl2, toml_segment_quoted(flags, j));
            if (esc == NULL) {
                free(prefix);
                PyErr_NoMemory();
                return -1;
            }
            strcat(prefix, esc);
            free(esc);
        }
        if (toml_set_add_str(ctx->implicit_tables, prefix) < 0) {
            free(prefix);
            return -1;
        }
        free(prefix);
    }
    return 0;
}

/* 键赋值：key... = value（parts 为当前表内相对路径）。 */
static int toml_apply_key_value(TomlCtx* ctx, PyObject* parts, PyObject* flags,
                                PyObject* value)
{
    Py_ssize_t n = PyList_GET_SIZE(parts);
    PyObject* container;
    PyObject* last_key;
    char* abs_path;
    int rc = -1;

    container = toml_navigate(ctx, ctx->current_table, parts, 1);
    if (container == NULL) return -1;
    last_key = PyList_GET_ITEM(parts, n - 1);

    abs_path = toml_abs_path_str(ctx, parts, flags);
    if (abs_path == NULL) return -1;

    if (toml_set_contains_str(ctx->defined_keys, abs_path) > 0) {
        toml_error(ctx, "duplicate key");
        free(abs_path);
        return -1;
    }
    if (toml_set_contains_str(ctx->defined_tables, abs_path) > 0 ||
        toml_set_contains_str(ctx->defined_arrays, abs_path) > 0) {
        toml_error(ctx, "key redefines a table");
        free(abs_path);
        return -1;
    }
    if (PyDict_SetItem(container, last_key, value) < 0) {
        free(abs_path);
        return -1;
    }
    if (toml_set_add_str(ctx->defined_keys, abs_path) < 0) {
        free(abs_path);
        return -1;
    }
    if (toml_mark_implicit(ctx, parts, flags) < 0) {
        free(abs_path);
        return -1;
    }
    free(abs_path);
    rc = 0;
    return rc;
}

/* [a.b] 表头。返回新引用容器 dict，失败返回 NULL（并置异常）。 */
static PyObject* toml_apply_table_header(TomlCtx* ctx, PyObject* parts,
                                         PyObject* flags)
{
    Py_ssize_t n = PyList_GET_SIZE(parts);
    char* full_path;
    PyObject* container;
    PyObject* last_key;
    PyObject* existing;

    if (n == 0) {
        toml_error(ctx, "empty table header");
        return NULL;
    }
    full_path = toml_path_str(parts, flags);
    if (full_path == NULL) return NULL;

    if (toml_set_contains_str(ctx->defined_tables, full_path) > 0) {
        toml_error(ctx, "duplicate table header");
        free(full_path);
        return NULL;
    }
    if (toml_set_contains_str(ctx->defined_arrays, full_path) > 0) {
        toml_error(ctx, "table header conflicts with array-of-tables");
        free(full_path);
        return NULL;
    }
    if (toml_set_contains_str(ctx->implicit_tables, full_path) > 0) {
        toml_error(ctx, "cannot redefine implicitly created table");
        free(full_path);
        return NULL;
    }
    if (toml_set_contains_str(ctx->defined_keys, full_path) > 0) {
        toml_error(ctx, "cannot redefine a key as a table");
        free(full_path);
        return NULL;
    }

    container = toml_navigate(ctx, ctx->root, parts, 1);
    if (container == NULL) {
        free(full_path);
        return NULL;
    }
    last_key = PyList_GET_ITEM(parts, n - 1);
    existing = PyDict_GetItemWithError(container, last_key);
    if (existing != NULL && !PyDict_Check(existing)) {
        toml_error(ctx, "cannot redefine a key as a table");
        free(full_path);
        return NULL;
    }
    if (existing == NULL) {
        PyObject* new_dict = PyDict_New();
        if (new_dict == NULL) {
            free(full_path);
            return NULL;
        }
        if (PyDict_SetItem(container, last_key, new_dict) < 0) {
            Py_DECREF(new_dict);
            free(full_path);
            return NULL;
        }
        Py_DECREF(new_dict);
    }
    if (toml_set_add_str(ctx->defined_tables, full_path) < 0) {
        free(full_path);
        return NULL;
    }
    if (toml_mark_implicit(ctx, parts, flags) < 0) {
        free(full_path);
        return NULL;
    }
    free(full_path);

    /* 返回最终表 dict（root 路径下的叶子容器），供调用者设为 current_table。
     * 注意：toml_navigate 返回的是 last_key 的父容器，不能直接用作新表。 */
    existing = PyDict_GetItemWithError(container, last_key);
    if (existing == NULL) {
        if (PyErr_Occurred()) {
            return NULL;
        }
        toml_error(ctx, "internal error: table was not created");
        return NULL;
    }
    Py_INCREF(existing);
    return existing;
}

/* [[a.b]] 表数组。返回新引用（追加的 dict），失败返回 NULL。 */
static PyObject* toml_apply_array_table(TomlCtx* ctx, PyObject* parts,
                                        PyObject* flags)
{
    Py_ssize_t n = PyList_GET_SIZE(parts);
    PyObject* cur = ctx->root;
    PyObject* last_key;
    PyObject* list_obj;
    PyObject* new_dict;
    char* full_path;

    if (n == 0) {
        toml_error(ctx, "empty array-of-tables header");
        return NULL;
    }
    full_path = toml_path_str(parts, flags);
    if (full_path == NULL) return NULL;

    if (toml_set_contains_str(ctx->defined_keys, full_path) > 0) {
        toml_error(ctx, "array table redefines a key");
        free(full_path);
        return NULL;
    }
    if (toml_set_contains_str(ctx->defined_tables, full_path) > 0) {
        toml_error(ctx, "cannot mix table and array-of-tables");
        free(full_path);
        return NULL;
    }
    if (toml_set_contains_str(ctx->implicit_tables, full_path) > 0) {
        toml_error(ctx, "cannot redefine implicitly created table");
        free(full_path);
        return NULL;
    }

    {
        Py_ssize_t i;
        for (i = 0; i < n - 1; i++) {
            PyObject* key = PyList_GET_ITEM(parts, i);
            PyObject* child = PyDict_GetItemWithError(cur, key);
            if (child == NULL) {
                if (PyErr_Occurred()) {
                    free(full_path);
                    return NULL;
                }
                child = PyDict_New();
                if (child == NULL) {
                    free(full_path);
                    return NULL;
                }
                if (PyDict_SetItem(cur, key, child) < 0) {
                    Py_DECREF(child);
                    free(full_path);
                    return NULL;
                }
                Py_DECREF(child);
            } else if (!PyDict_Check(child)) {
                toml_error(ctx, "cannot extend a key as a table");
                free(full_path);
                return NULL;
            }
            cur = child;
        }
    }

    last_key = PyList_GET_ITEM(parts, n - 1);
    list_obj = PyDict_GetItemWithError(cur, last_key);
    if (list_obj == NULL) {
        if (PyErr_Occurred()) {
            free(full_path);
            return NULL;
        }
        list_obj = PyList_New(0);
        if (list_obj == NULL) {
            free(full_path);
            return NULL;
        }
        if (PyDict_SetItem(cur, last_key, list_obj) < 0) {
            Py_DECREF(list_obj);
            free(full_path);
            return NULL;
        }
        Py_DECREF(list_obj);
    } else if (!PyList_Check(list_obj)) {
        toml_error(ctx, "key is not an array of tables");
        free(full_path);
        return NULL;
    }

    new_dict = PyDict_New();
    if (new_dict == NULL) {
        free(full_path);
        return NULL;
    }
    if (PyList_Append(list_obj, new_dict) < 0) {
        Py_DECREF(new_dict);
        free(full_path);
        return NULL;
    }
    Py_DECREF(new_dict);

    if (toml_set_add_str(ctx->defined_arrays, full_path) < 0) {
        free(full_path);
        return NULL;
    }
    if (toml_mark_implicit(ctx, parts, flags) < 0) {
        free(full_path);
        return NULL;
    }
    free(full_path);

    /* 更新当前表路径为 parts + [<index>]，使不同数组元素内的同名键
     * （如 [[p]] 下两个元素的 n）不会因绝对路径相同而误判为重复键。
     * 同步更新引号标志：新增的索引段视为裸段（flag=0）。 */
    {
        Py_ssize_t idx = PyList_GET_SIZE(list_obj) - 1;
        Py_ssize_t i;
        PyObject* idx_str = PyUnicode_FromFormat("[%zd]", idx);
        PyObject* new_path;
        PyObject* new_flags;
        if (idx_str == NULL) {
            return NULL;
        }
        new_path = PyList_New(n + 1);
        new_flags = PyList_New(n + 1);
        if (new_path == NULL || new_flags == NULL) {
            Py_XDECREF(new_path);
            Py_XDECREF(new_flags);
            Py_DECREF(idx_str);
            return NULL;
        }
        for (i = 0; i < n; i++) {
            PyObject* item = PyList_GET_ITEM(parts, i);
            PyObject* flag = flags ? PyList_GET_ITEM(flags, i) : Py_False;
            Py_INCREF(item);
            Py_INCREF(flag);
            PyList_SET_ITEM(new_path, i, item);
            PyList_SET_ITEM(new_flags, i, flag);
        }
        PyList_SET_ITEM(new_path, n, idx_str);
        PyList_SET_ITEM(new_flags, n, Py_False);
        Py_INCREF(Py_False);
        Py_DECREF(ctx->current_table_path);
        ctx->current_table_path = new_path;
        Py_XDECREF(ctx->current_table_path_flags);
        ctx->current_table_path_flags = new_flags;
    }

    Py_INCREF(new_dict);
    return new_dict;
}

/* ------------------------------------------------------------------ */
/* 顶层入口                                                             */
/* ------------------------------------------------------------------ */

PyObject* cfgdrift_parse_toml_text(const char* text, Py_ssize_t len)
{
    TomlCtx ctx;
    int rc = -1;
    PyObject* result = NULL;

    memset(&ctx, 0, sizeof(ctx));
    ctx.text = text;
    ctx.len = len;
    ctx.pos = 0;

    ctx.root = PyDict_New();
    ctx.current_table_path = PyList_New(0);
    ctx.current_table_path_flags = PyList_New(0);
    ctx.defined_keys = PySet_New(NULL);
    ctx.defined_tables = PySet_New(NULL);
    ctx.defined_arrays = PySet_New(NULL);
    ctx.implicit_tables = PySet_New(NULL);
    if (ctx.root == NULL || ctx.current_table_path == NULL ||
        ctx.current_table_path_flags == NULL ||
        ctx.defined_keys == NULL || ctx.defined_tables == NULL ||
        ctx.defined_arrays == NULL || ctx.implicit_tables == NULL) {
        Py_XDECREF(ctx.root);
        Py_XDECREF(ctx.current_table_path);
        Py_XDECREF(ctx.current_table_path_flags);
        Py_XDECREF(ctx.defined_keys);
        Py_XDECREF(ctx.defined_tables);
        Py_XDECREF(ctx.defined_arrays);
        Py_XDECREF(ctx.implicit_tables);
        return NULL;
    }
    ctx.current_table = ctx.root;
    Py_INCREF(ctx.current_table);

    while (ctx.pos < ctx.len) {
        toml_skip_ws_comments(&ctx);
        if (ctx.pos >= ctx.len) break;

        if (ctx.text[ctx.pos] == '[') {
            int is_array_table = 0;
            PyObject* parts;
            PyObject* flags;
            PyObject* new_table = NULL;
            ctx.pos++;
            if (ctx.pos < ctx.len && ctx.text[ctx.pos] == '[') {
                is_array_table = 1;
                ctx.pos++;
            }
            parts = toml_parse_key_path(&ctx, 0, &flags);
            if (parts == NULL) goto done;
            toml_skip_ws(&ctx);
            if (ctx.pos >= ctx.len || ctx.text[ctx.pos] != ']') {
                toml_error(&ctx, "expected ']' in table header");
                Py_DECREF(parts);
                Py_DECREF(flags);
                goto done;
            }
            ctx.pos++;
            if (is_array_table) {
                if (ctx.pos < ctx.len && ctx.text[ctx.pos] == ']') {
                    ctx.pos++;
                } else {
                    toml_error(&ctx, "expected ']]' in array-of-tables header");
                    Py_DECREF(parts);
                    Py_DECREF(flags);
                    goto done;
                }
            }
            toml_skip_ws_comments(&ctx);

            if (is_array_table) {
                new_table = toml_apply_array_table(&ctx, parts, flags);
                Py_DECREF(parts);
                Py_DECREF(flags);
                /* toml_apply_array_table 已更新 current_table_path 及引号标志 */
            } else {
                new_table = toml_apply_table_header(&ctx, parts, flags);
                if (new_table == NULL) {
                    Py_DECREF(parts);
                    Py_DECREF(flags);
                    goto done;
                }
                Py_DECREF(ctx.current_table_path);
                ctx.current_table_path = parts;
                Py_INCREF(ctx.current_table_path);
                Py_DECREF(parts);
                Py_XDECREF(ctx.current_table_path_flags);
                ctx.current_table_path_flags = flags;
                Py_INCREF(ctx.current_table_path_flags);
                Py_DECREF(flags);
            }
            if (new_table == NULL) {
                goto done;
            }
            /* 更新当前表 */
            Py_DECREF(ctx.current_table);
            ctx.current_table = new_table;
            continue;
        }

        /* 键赋值行 */
        {
            PyObject* parts;
            PyObject* flags;
            PyObject* value;
            parts = toml_parse_key_path(&ctx, 1, &flags);
            if (parts == NULL) goto done;
            toml_skip_ws(&ctx);
            if (ctx.pos >= ctx.len || ctx.text[ctx.pos] != '=') {
                toml_error(&ctx, "expected '=' after key");
                Py_DECREF(parts);
                Py_DECREF(flags);
                goto done;
            }
            ctx.pos++;
            value = toml_parse_value(&ctx);
            if (value == NULL) {
                Py_DECREF(parts);
                Py_DECREF(flags);
                goto done;
            }
            rc = toml_apply_key_value(&ctx, parts, flags, value);
            Py_DECREF(value);
            Py_DECREF(parts);
            Py_DECREF(flags);
            if (rc < 0) goto done;
            toml_skip_ws_comments(&ctx);
        }
    }

    /* 成功路径：result 持有 root 的唯一引用（current_table 的引用在
     * done 块中释放），不再额外 INCREF，避免引用泄漏。 */
    result = ctx.root;
    ctx.root = NULL;
    rc = 0;

done:
    Py_XDECREF(ctx.root);
    Py_XDECREF(ctx.current_table);
    Py_XDECREF(ctx.current_table_path);
    Py_XDECREF(ctx.current_table_path_flags);
    Py_XDECREF(ctx.defined_keys);
    Py_XDECREF(ctx.defined_tables);
    Py_XDECREF(ctx.defined_arrays);
    Py_XDECREF(ctx.implicit_tables);
    if (rc < 0) {
        Py_XDECREF(result);
        return NULL;
    }
    return result;
}
