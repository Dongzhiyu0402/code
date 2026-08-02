/*
 * parser_json.c — JSON 递归下降解析器（RFC 8259 子集）。
 *
 * 支持：对象、数组、字符串（含 \uXXXX 代理对）、数字（int/float、e/E 指数、负数）、
 *       true / false / null。
 * 行为：重复键 last-wins；拒绝尾随逗号、裸单引号。
 * 错误：统一 ValueError("parse error at line L, column C: <msg>")，L/C 1-based。
 */

#include <Python.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

extern void cfgdrift_raise_error(const char* msg, int line, int col);
extern void cfgdrift_line_col(const char* text, Py_ssize_t offset,
                              int* line, int* col);
extern PyObject* cfgdrift_new_str(const char* s, Py_ssize_t len);

/* 解析器上下文：输入 + 当前偏移量 + 嵌套深度。 */
#define JSON_MAX_DEPTH 512

typedef struct {
    const char* text;
    Py_ssize_t len;
    Py_ssize_t pos;
    int depth;
} JsonCtx;

static void json_skip_ws(JsonCtx* ctx)
{
    while (ctx->pos < ctx->len) {
        char c = ctx->text[ctx->pos];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            ctx->pos++;
        } else {
            break;
        }
    }
}

static void json_error(JsonCtx* ctx, const char* msg)
{
    int line, col;
    cfgdrift_line_col(ctx->text, ctx->pos, &line, &col);
    cfgdrift_raise_error(msg, line, col);
}

static PyObject* json_parse_value(JsonCtx* ctx);

/* 将 4 个十六进制字符解析为码点；失败返回 -1。 */
static int json_hex4(const char* s)
{
    int i, v = 0;
    for (i = 0; i < 4; i++) {
        char c = s[i];
        int d;
        if (c >= '0' && c <= '9') {
            d = c - '0';
        } else if (c >= 'a' && c <= 'f') {
            d = c - 'a' + 10;
        } else if (c >= 'A' && c <= 'F') {
            d = c - 'A' + 10;
        } else {
            return -1;
        }
        v = (v << 4) | d;
    }
    return v;
}

/*
 * 解析 JSON 字符串（当前位于引号之后）。
 * 返回 PyUnicode（UTF-8 编码），失败返回 NULL。
 * 处理 \uXXXX 与 \uXXXX\uXXXX 代理对。
 */
static PyObject* json_parse_string(JsonCtx* ctx)
{
    /* 输出缓冲（UTF-8 字节），动态扩容。 */
    char* buf = NULL;
    Py_ssize_t cap = 64;
    Py_ssize_t out = 0;
    PyObject* result = NULL;

    buf = (char*)malloc((size_t)cap);
    if (buf == NULL) {
        PyErr_NoMemory();
        return NULL;
    }

    while (ctx->pos < ctx->len) {
        unsigned char c = (unsigned char)ctx->text[ctx->pos];
        if (c == '"') {
            ctx->pos++;
            result = cfgdrift_new_str(buf, out);
            free(buf);
            return result;
        }
        if (c == '\\') {
            ctx->pos++;
            if (ctx->pos >= ctx->len) {
                json_error(ctx, "unterminated escape sequence");
                free(buf);
                return NULL;
            }
            c = (unsigned char)ctx->text[ctx->pos];
            ctx->pos++;
            switch (c) {
            case '"': c = '"'; break;
            case '\\': c = '\\'; break;
            case '/': c = '/'; break;
            case 'b': c = '\b'; break;
            case 'f': c = '\f'; break;
            case 'n': c = '\n'; break;
            case 'r': c = '\r'; break;
            case 't': c = '\t'; break;
            case 'u': {
                /* \uXXXX，可能带代理对。 */
                int cp;
                if (ctx->pos + 4 > ctx->len) {
                    json_error(ctx, "invalid \\u escape");
                    free(buf);
                    return NULL;
                }
                cp = json_hex4(ctx->text + ctx->pos);
                if (cp < 0) {
                    json_error(ctx, "invalid \\u escape");
                    free(buf);
                    return NULL;
                }
                ctx->pos += 4;
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    /* 高代理：必须紧跟低代理。 */
                    int lo;
                    if (ctx->pos + 6 <= ctx->len &&
                        ctx->text[ctx->pos] == '\\' &&
                        ctx->text[ctx->pos + 1] == 'u') {
                        lo = json_hex4(ctx->text + ctx->pos + 2);
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            ctx->pos += 6;
                        } else {
                            json_error(ctx, "unpaired surrogate in \\u escape");
                            free(buf);
                            return NULL;
                        }
                    } else {
                        json_error(ctx, "unpaired surrogate in \\u escape");
                        free(buf);
                        return NULL;
                    }
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    json_error(ctx, "unpaired surrogate in \\u escape");
                    free(buf);
                    return NULL;
                }
                /* 将码点编码为 UTF-8 字节。 */
                {
                    unsigned char utf8[4];
                    int n = 0;
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
                    if (out + n > cap) {
                        char* nb;
                        while (out + n > cap) {
                            cap *= 2;
                        }
                        nb = (char*)realloc(buf, (size_t)cap);
                        if (nb == NULL) {
                            PyErr_NoMemory();
                            free(buf);
                            return NULL;
                        }
                        buf = nb;
                    }
                    memcpy(buf + out, utf8, (size_t)n);
                    out += n;
                }
                continue; /* 已写入 UTF-8，跳过通用写入 */
            }
            default:
                json_error(ctx, "invalid escape character");
                free(buf);
                return NULL;
            }
        } else if (c < 0x20) {
            json_error(ctx, "unescaped control character in string");
            free(buf);
            return NULL;
        } else {
            ctx->pos++;
        }
        /* 通用单字节写入（含多字节 UTF-8 原样拷贝）。 */
        if (out + 1 > cap) {
            char* nb;
            cap *= 2;
            nb = (char*)realloc(buf, (size_t)cap);
            if (nb == NULL) {
                PyErr_NoMemory();
                free(buf);
                return NULL;
            }
            buf = nb;
        }
        buf[out++] = (char)c;
    }

    json_error(ctx, "unterminated string");
    free(buf);
    return NULL;
}

/* 解析数字。当前位于数字首字符。 */
static PyObject* json_parse_number(JsonCtx* ctx)
{
    Py_ssize_t start = ctx->pos;
    int is_float = 0;

    if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '-') {
        ctx->pos++;
    }
    /* 整数部分 */
    if (ctx->pos >= ctx->len) {
        json_error(ctx, "invalid number");
        return NULL;
    }
    if (ctx->text[ctx->pos] == '0') {
        ctx->pos++;
    } else if (ctx->text[ctx->pos] >= '1' && ctx->text[ctx->pos] <= '9') {
        while (ctx->pos < ctx->len &&
               ctx->text[ctx->pos] >= '0' && ctx->text[ctx->pos] <= '9') {
            ctx->pos++;
        }
    } else {
        json_error(ctx, "invalid number");
        return NULL;
    }
    /* 小数部分 */
    if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '.') {
        is_float = 1;
        ctx->pos++;
        if (ctx->pos >= ctx->len ||
            ctx->text[ctx->pos] < '0' || ctx->text[ctx->pos] > '9') {
            json_error(ctx, "invalid number: expected digit after decimal point");
            return NULL;
        }
        while (ctx->pos < ctx->len &&
               ctx->text[ctx->pos] >= '0' && ctx->text[ctx->pos] <= '9') {
            ctx->pos++;
        }
    }
    /* 指数部分 */
    if (ctx->pos < ctx->len &&
        (ctx->text[ctx->pos] == 'e' || ctx->text[ctx->pos] == 'E')) {
        is_float = 1;
        ctx->pos++;
        if (ctx->pos < ctx->len &&
            (ctx->text[ctx->pos] == '+' || ctx->text[ctx->pos] == '-')) {
            ctx->pos++;
        }
        if (ctx->pos >= ctx->len ||
            ctx->text[ctx->pos] < '0' || ctx->text[ctx->pos] > '9') {
            json_error(ctx, "invalid number: expected digit in exponent");
            return NULL;
        }
        while (ctx->pos < ctx->len &&
               ctx->text[ctx->pos] >= '0' && ctx->text[ctx->pos] <= '9') {
            ctx->pos++;
        }
    }
    /* 构造临时字符串并解析 */
    {
        Py_ssize_t n = ctx->pos - start;
        char* tmp = (char*)malloc((size_t)n + 1);
        PyObject* obj;
        int consumed;
        if (tmp == NULL) {
            PyErr_NoMemory();
            return NULL;
        }
        memcpy(tmp, ctx->text + start, (size_t)n);
        tmp[n] = '\0';
        obj = PyLong_FromString(tmp, NULL, 10);
        if (obj == NULL) {
            PyErr_Clear();
            if (is_float) {
                PyObject* u = PyUnicode_FromStringAndSize(tmp, n);
                if (u == NULL) {
                    free(tmp);
                    return NULL;
                }
                obj = PyFloat_FromString(u);
                Py_DECREF(u);
            } else {
                json_error(ctx, "invalid number");
            }
        }
        free(tmp);
        (void)consumed;
        if (obj == NULL && !PyErr_Occurred()) {
            json_error(ctx, "invalid number");
        }
        return obj;
    }
}

static PyObject* json_parse_array(JsonCtx* ctx)
{
    PyObject* list;

    /* 当前位于 '[' 之后 */
    list = PyList_New(0);
    if (list == NULL) {
        return NULL;
    }
    json_skip_ws(ctx);
    if (ctx->pos < ctx->len && ctx->text[ctx->pos] == ']') {
        ctx->pos++;
        return list;
    }
    for (;;) {
        PyObject* item;
        json_skip_ws(ctx);
        item = json_parse_value(ctx);
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
        json_skip_ws(ctx);
        if (ctx->pos >= ctx->len) {
            json_error(ctx, "unterminated array");
            Py_DECREF(list);
            return NULL;
        }
        if (ctx->text[ctx->pos] == ',') {
            ctx->pos++;
            json_skip_ws(ctx);
            if (ctx->pos < ctx->len && ctx->text[ctx->pos] == ']') {
                json_error(ctx, "trailing comma in array");
                Py_DECREF(list);
                return NULL;
            }
            continue;
        }
        if (ctx->text[ctx->pos] == ']') {
            ctx->pos++;
            return list;
        }
        json_error(ctx, "expected ',' or ']' in array");
        Py_DECREF(list);
        return NULL;
    }
}

static PyObject* json_parse_object(JsonCtx* ctx)
{
    PyObject* dict;

    dict = PyDict_New();
    if (dict == NULL) {
        return NULL;
    }
    json_skip_ws(ctx);
    if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '}') {
        ctx->pos++;
        return dict;
    }
    for (;;) {
        PyObject* key = NULL;
        PyObject* value = NULL;
        json_skip_ws(ctx);
        if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '\'') {
            json_error(ctx, "bare single quotes are not allowed in JSON");
            Py_DECREF(dict);
            return NULL;
        }
        if (ctx->pos >= ctx->len || ctx->text[ctx->pos] != '"') {
            json_error(ctx, "expected string key in object");
            Py_DECREF(dict);
            return NULL;
        }
        ctx->pos++;
        key = json_parse_string(ctx);
        if (key == NULL) {
            Py_DECREF(dict);
            return NULL;
        }
        json_skip_ws(ctx);
        if (ctx->pos >= ctx->len || ctx->text[ctx->pos] != ':') {
            Py_DECREF(key);
            Py_DECREF(dict);
            json_error(ctx, "expected ':' after object key");
            return NULL;
        }
        ctx->pos++;
        json_skip_ws(ctx);
        value = json_parse_value(ctx);
        if (value == NULL) {
            Py_DECREF(key);
            Py_DECREF(dict);
            return NULL;
        }
        /* last-wins：重复键覆盖旧值 */
        if (PyDict_SetItem(dict, key, value) < 0) {
            Py_DECREF(key);
            Py_DECREF(value);
            Py_DECREF(dict);
            return NULL;
        }
        Py_DECREF(key);
        Py_DECREF(value);
        json_skip_ws(ctx);
        if (ctx->pos >= ctx->len) {
            json_error(ctx, "unterminated object");
            Py_DECREF(dict);
            return NULL;
        }
        if (ctx->text[ctx->pos] == ',') {
            ctx->pos++;
            json_skip_ws(ctx);
            if (ctx->pos < ctx->len && ctx->text[ctx->pos] == '}') {
                json_error(ctx, "trailing comma in object");
                Py_DECREF(dict);
                return NULL;
            }
            continue;
        }
        if (ctx->text[ctx->pos] == '}') {
            ctx->pos++;
            return dict;
        }
        json_error(ctx, "expected ',' or '}' in object");
        Py_DECREF(dict);
        return NULL;
    }
}

static PyObject* json_parse_value(JsonCtx* ctx)
{
    PyObject* result = NULL;
    json_skip_ws(ctx);
    if (ctx->pos >= ctx->len) {
        json_error(ctx, "unexpected end of input");
        return NULL;
    }
    if (ctx->depth >= JSON_MAX_DEPTH) {
        json_error(ctx, "max depth exceeded");
        return NULL;
    }
    ctx->depth++;
    switch (ctx->text[ctx->pos]) {
    case '{':
        ctx->pos++;
        result = json_parse_object(ctx);
        break;
    case '[':
        ctx->pos++;
        result = json_parse_array(ctx);
        break;
    case '"':
        ctx->pos++;
        result = json_parse_string(ctx);
        break;
    case 't':
        if (ctx->len - ctx->pos >= 4 && memcmp(ctx->text + ctx->pos, "true", 4) == 0) {
            ctx->pos += 4;
            Py_INCREF(Py_True);
            result = Py_True;
            break;
        }
        break;
    case 'f':
        if (ctx->len - ctx->pos >= 5 && memcmp(ctx->text + ctx->pos, "false", 5) == 0) {
            ctx->pos += 5;
            Py_INCREF(Py_False);
            result = Py_False;
            break;
        }
        break;
    case 'n':
        if (ctx->len - ctx->pos >= 4 && memcmp(ctx->text + ctx->pos, "null", 4) == 0) {
            ctx->pos += 4;
            Py_INCREF(Py_None);
            result = Py_None;
            break;
        }
        break;
    case '\'':
        json_error(ctx, "bare single quotes are not allowed in JSON");
        break;
    case '-':
    case '0':
    case '1':
    case '2':
    case '3':
    case '4':
    case '5':
    case '6':
    case '7':
    case '8':
    case '9':
        result = json_parse_number(ctx);
        break;
    default:
        break;
    }
    ctx->depth--;
    if (result == NULL && !PyErr_Occurred()) {
        json_error(ctx, "unexpected character in JSON value");
        return NULL;
    }
    return result;
}

/* 顶层入口：解析完整 JSON 文档，必须为单个值，允许前后空白。 */
PyObject* cfgdrift_parse_json_text(const char* text, Py_ssize_t len)
{
    JsonCtx ctx;
    PyObject* result;

    ctx.text = text;
    ctx.len = len;
    ctx.pos = 0;
    ctx.depth = 0;

    json_skip_ws(&ctx);
    result = json_parse_value(&ctx);
    if (result == NULL) {
        return NULL;
    }
    json_skip_ws(&ctx);
    if (ctx.pos != ctx.len) {
        json_error(&ctx, "trailing characters after JSON value");
        Py_DECREF(result);
        return NULL;
    }
    return result;
}
