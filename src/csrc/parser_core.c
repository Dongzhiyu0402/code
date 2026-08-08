/*
 * parser_core.c — cfgdrift._cfgdrift 扩展：模块初始化、方法表、公共工具。
 *
 * 纯 C99 标准库实现，无 POSIX / Windows 专属头，MSVC / gcc / clang 均可编译。
 * 暴露函数：
 *   parse_json(text: str) -> dict
 *   parse_toml(text: str) -> dict
 *   parse_ini(text: str) -> dict
 *   version() -> str
 *
 * 解析器实现在 parser_json.c / parser_toml.c / parser_ini.c。
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* 公共工具                                                             */
/* ------------------------------------------------------------------ */

/* 抛出 ValueError("parse error at line L, column C: msg")。L/C 为 1-based。 */
void cfgdrift_raise_error(const char* msg, int line, int col)
{
    char buffer[512];
    snprintf(buffer, sizeof(buffer), "parse error at line %d, column %d: %s",
             line, col, msg);
    PyErr_SetString(PyExc_ValueError, buffer);
}

/* 由输入字节串与偏移量计算 1-based 行列号。 */
void cfgdrift_line_col(const char* text, Py_ssize_t offset,
                       int* line, int* col)
{
    int l = 1;
    int c = 1;
    Py_ssize_t i;
    for (i = 0; i < offset && text[i] != '\0'; i++) {
        if (text[i] == '\n') {
            l++;
            c = 1;
        } else {
            c++;
        }
    }
    *line = l;
    *col = c;
}

/* UTF-8 字节串 -> PyUnicode。失败返回 NULL。 */
PyObject* cfgdrift_new_str(const char* s, Py_ssize_t len)
{
    if (s == NULL) {
        return PyUnicode_FromStringAndSize("", 0);
    }
    return PyUnicode_FromStringAndSize(s, len);
}

/*
 * 解析数字字面量。成功时返回新引用 PyLong / PyFloat，*consumed 置为消耗字节数；
 * 失败返回 NULL（已设置异常）。
 * 仅处理 C 解析器已确认合法的数字（JSON 数字或 TOML 数字归一化后的十进制）。
 */
PyObject* cfgdrift_parse_number(const char* s, Py_ssize_t len, int* consumed)
{
    Py_ssize_t i = 0;
    int is_float = 0;
    char* end = NULL;

    if (len <= 0) {
        PyErr_SetString(PyExc_ValueError, "empty number literal");
        return NULL;
    }
    /* 跳过前导符号 */
    if (s[i] == '+' || s[i] == '-') {
        i++;
    }
    for (; i < len; i++) {
        char c = s[i];
        if (c == '.' || c == 'e' || c == 'E') {
            is_float = 1;
        }
        if (!((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' ||
              c == '+' || c == '-')) {
            break;
        }
    }
    if (is_float) {
        double d = strtod(s, &end);
        if (end == s) {
            PyErr_SetString(PyExc_ValueError, "invalid float literal");
            return NULL;
        }
        *consumed = (int)(end - s);
        return PyFloat_FromDouble(d);
    } else {
        long long v = strtoll(s, &end, 10);
        if (end == s) {
            PyErr_SetString(PyExc_ValueError, "invalid integer literal");
            return NULL;
        }
        *consumed = (int)(end - s);
        return PyLong_FromLongLong(v);
    }
}

/* ------------------------------------------------------------------ */
/* 各格式解析入口（定义在对应 .c 文件中）                                 */
/* ------------------------------------------------------------------ */

PyObject* cfgdrift_parse_json_text(const char* text, Py_ssize_t len);
PyObject* cfgdrift_parse_toml_text(const char* text, Py_ssize_t len);
PyObject* cfgdrift_parse_ini_text(const char* text, Py_ssize_t len);

/* ------------------------------------------------------------------ */
/* 模块方法包装                                                         */
/* ------------------------------------------------------------------ */

static PyObject* _cfgdrift_parse_json(PyObject* self, PyObject* args)
{
    const char* text;
    Py_ssize_t len;
    (void)self;
    if (!PyArg_ParseTuple(args, "s#:parse_json", &text, &len)) {
        return NULL;
    }
    return cfgdrift_parse_json_text(text, len);
}

static PyObject* _cfgdrift_parse_toml(PyObject* self, PyObject* args)
{
    const char* text;
    Py_ssize_t len;
    (void)self;
    if (!PyArg_ParseTuple(args, "s#:parse_toml", &text, &len)) {
        return NULL;
    }
    return cfgdrift_parse_toml_text(text, len);
}

static PyObject* _cfgdrift_parse_ini(PyObject* self, PyObject* args)
{
    const char* text;
    Py_ssize_t len;
    (void)self;
    if (!PyArg_ParseTuple(args, "s#:parse_ini", &text, &len)) {
        return NULL;
    }
    return cfgdrift_parse_ini_text(text, len);
}

static PyObject* _cfgdrift_version(PyObject* self, PyObject* args)
{
    (void)self;
    (void)args;
    return PyUnicode_FromString("0.11.0-c");
}

static PyMethodDef _cfgdrift_methods[] = {
    {"parse_json", _cfgdrift_parse_json, METH_VARARGS,
     "parse_json(text: str) -> dict\n\nParse a JSON document into a semantic tree."},
    {"parse_toml", _cfgdrift_parse_toml, METH_VARARGS,
     "parse_toml(text: str) -> dict\n\nParse a TOML document into a semantic tree."},
    {"parse_ini", _cfgdrift_parse_ini, METH_VARARGS,
     "parse_ini(text: str) -> dict\n\nParse an INI document into a semantic tree."},
    {"version", _cfgdrift_version, METH_NOARGS,
     "version() -> str\n\nReturn the C extension version string."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef _cfgdrift_module = {
    PyModuleDef_HEAD_INIT,
    "_cfgdrift",
    "cfgdrift C core: JSON/TOML/INI parsers.",
    -1,
    _cfgdrift_methods
};

PyMODINIT_FUNC PyInit__cfgdrift(void)
{
    return PyModule_Create(&_cfgdrift_module);
}
