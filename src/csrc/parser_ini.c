/*
 * parser_ini.c — INI 解析器。
 *
 * 支持：`[section]`、`key=value` / `key:value`、整行注释 `#` / `;`、
 *       键大小写保留、值去首尾空白并剥配对引号。
 * 行为：重复键 last-wins；无 section 的键置于顶层；不支持行尾注释。
 * 错误：统一 ValueError("parse error at line L, column C: <msg>")。
 */

#include <Python.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

extern void cfgdrift_raise_error(const char* msg, int line, int col);
extern void cfgdrift_line_col(const char* text, Py_ssize_t offset,
                              int* line, int* col);
extern PyObject* cfgdrift_new_str(const char* s, Py_ssize_t len);

typedef struct {
    const char* text;
    Py_ssize_t len;
    Py_ssize_t pos;
} IniCtx;

/* 当前行号（1-based，从解析起点计）。 */
static int ini_line(const IniCtx* ctx)
{
    int line = 1;
    Py_ssize_t i;
    for (i = 0; i < ctx->pos && i < ctx->len; i++) {
        if (ctx->text[i] == '\n') {
            line++;
        }
    }
    return line;
}

static void ini_error(IniCtx* ctx, const char* msg)
{
    int line, col;
    cfgdrift_line_col(ctx->text, ctx->pos, &line, &col);
    cfgdrift_raise_error(msg, line, col);
}

static int ini_is_ws(char c)
{
    return c == ' ' || c == '\t' || c == '\r';
}

/* 去掉值两侧空白；若被成对引号包裹则剥离。引号内空白保留。 */
static char* ini_trim_value(const char* s, Py_ssize_t len)
{
    Py_ssize_t start = 0, end = len;
    char* out;

    while (start < end && ini_is_ws(s[start])) {
        start++;
    }
    while (end > start && ini_is_ws(s[end - 1])) {
        end--;
    }
    if (end - start >= 2 &&
        ((s[start] == '"' && s[end - 1] == '"') ||
         (s[start] == '\'' && s[end - 1] == '\''))) {
        start++;
        end--;
    }
    out = (char*)malloc((size_t)(end - start) + 1);
    if (out == NULL) {
        return NULL;
    }
    memcpy(out, s + start, (size_t)(end - start));
    out[end - start] = '\0';
    return out;
}

/* 解析整份 INI 文本。 */
PyObject* cfgdrift_parse_ini_text(const char* text, Py_ssize_t len)
{
    IniCtx ctx;
    PyObject* root;
    PyObject* current;

    ctx.text = text;
    ctx.len = len;
    ctx.pos = 0;

    root = PyDict_New();
    if (root == NULL) {
        return NULL;
    }
    current = root;
    Py_INCREF(current);

    while (ctx.pos < ctx.len) {
        Py_ssize_t line_start = ctx.pos;
        Py_ssize_t line_end;
        Py_ssize_t i;
        int lnum;

        /* 找到本行结尾（不含换行）。 */
        line_end = ctx.pos;
        while (line_end < ctx.len && text[line_end] != '\n') {
            line_end++;
        }
        lnum = ini_line(&ctx);

        /* 跳过行首空白。 */
        i = line_start;
        while (i < line_end && ini_is_ws(text[i])) {
            i++;
        }

        if (i < line_end) {
            char c = text[i];
            if (c == '#' || c == ';') {
                /* 整行注释 */
            } else if (c == '[') {
                /* section 头 */
                Py_ssize_t j = i + 1;
                Py_ssize_t name_start, name_end;
                PyObject* sec;
                PyObject* existing;

                while (j < line_end && ini_is_ws(text[j])) {
                    j++;
                }
                name_start = j;
                while (j < line_end && text[j] != ']') {
                    j++;
                }
                if (j >= line_end) {
                    ctx.pos = line_end;
                    ini_error(&ctx, "unterminated section header");
                    Py_DECREF(current);
                    Py_DECREF(root);
                    return NULL;
                }
                name_end = j;
                /* 检查 ']' 之后只允许空白 */
                {
                    Py_ssize_t k = j + 1;
                    while (k < line_end && ini_is_ws(text[k])) {
                        k++;
                    }
                    if (k != line_end) {
                        ctx.pos = line_end;
                        ini_error(&ctx, "unexpected content after section header");
                        Py_DECREF(current);
                        Py_DECREF(root);
                        return NULL;
                    }
                }
                sec = cfgdrift_new_str(text + name_start, name_end - name_start);
                if (sec == NULL) {
                    Py_DECREF(current);
                    Py_DECREF(root);
                    return NULL;
                }
                existing = PyDict_GetItemWithError(root, sec);
                if (existing == NULL && PyErr_Occurred()) {
                    Py_DECREF(sec);
                    Py_DECREF(current);
                    Py_DECREF(root);
                    return NULL;
                }
                if (existing == NULL) {
                    PyObject* newsec = PyDict_New();
                    if (newsec == NULL) {
                        Py_DECREF(sec);
                        Py_DECREF(current);
                        Py_DECREF(root);
                        return NULL;
                    }
                    if (PyDict_SetItem(root, sec, newsec) < 0) {
                        Py_DECREF(newsec);
                        Py_DECREF(sec);
                        Py_DECREF(current);
                        Py_DECREF(root);
                        return NULL;
                    }
                    Py_DECREF(newsec);
                    existing = newsec;
                }
                /* 切换到该 section；重复 section 头 = 合并（last-wins 语义下继续写入） */
                Py_DECREF(current);
                Py_INCREF(existing);
                current = existing;
                Py_DECREF(sec);
            } else {
                /* key=value 或 key:value */
                Py_ssize_t eq = i;
                while (eq < line_end && text[eq] != '=' && text[eq] != ':') {
                    eq++;
                }
                if (eq >= line_end) {
                    ctx.pos = line_end;
                    ini_error(&ctx, "expected '=' or ':' in key-value line");
                    Py_DECREF(current);
                    Py_DECREF(root);
                    return NULL;
                }
                {
                    Py_ssize_t key_start = i;
                    Py_ssize_t key_end = eq;
                    PyObject* key;
                    char* val;
                    PyObject* pval;

                    /* 键去尾空白 */
                    while (key_end > key_start && ini_is_ws(text[key_end - 1])) {
                        key_end--;
                    }
                    if (key_end <= key_start) {
                        ctx.pos = line_end;
                        ini_error(&ctx, "empty key");
                        Py_DECREF(current);
                        Py_DECREF(root);
                        return NULL;
                    }
                    key = cfgdrift_new_str(text + key_start, key_end - key_start);
                    if (key == NULL) {
                        Py_DECREF(current);
                        Py_DECREF(root);
                        return NULL;
                    }
                    val = ini_trim_value(text + eq + 1, line_end - (eq + 1));
                    if (val == NULL) {
                        Py_DECREF(key);
                        Py_DECREF(current);
                        Py_DECREF(root);
                        PyErr_NoMemory();
                        return NULL;
                    }
                    pval = PyUnicode_FromString(val);
                    free(val);
                    if (pval == NULL) {
                        Py_DECREF(key);
                        Py_DECREF(current);
                        Py_DECREF(root);
                        return NULL;
                    }
                    /* last-wins */
                    if (PyDict_SetItem(current, key, pval) < 0) {
                        Py_DECREF(pval);
                        Py_DECREF(key);
                        Py_DECREF(current);
                        Py_DECREF(root);
                        return NULL;
                    }
                    Py_DECREF(pval);
                    Py_DECREF(key);
                }
            }
        }

        /* 前进到下一行 */
        if (line_end < ctx.len) {
            ctx.pos = line_end + 1; /* 跳过 \n */
        } else {
            ctx.pos = line_end;
        }
    }

    Py_DECREF(current);
    return root;
}
