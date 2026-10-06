#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace kessler::json {

const Value* Value::find(std::string_view key) const {
  if (type_ != Type::Object) return nullptr;
  for (const auto& m : o_) {
    if (m.first == key) return &m.second;
  }
  return nullptr;
}

const char* type_name(Value::Type t) {
  switch (t) {
    case Value::Type::Null: return "null";
    case Value::Type::Bool: return "boolean";
    case Value::Type::Number: return "number";
    case Value::Type::String: return "string";
    case Value::Type::Array: return "array";
    case Value::Type::Object: return "object";
  }
  return "unknown";
}

namespace {

class Parser {
 public:
  Parser(std::string_view text, int max_depth) : t_(text), max_depth_(max_depth) {}

  Value parse_document() {
    skip_ws();
    Value v = parse_value(0);
    skip_ws();
    if (pos_ != t_.size()) fail("trailing characters after JSON document");
    return v;
  }

 private:
  [[noreturn]] void fail(const char* what) const {
    char buf[160];
    std::snprintf(buf, sizeof buf, "invalid JSON at offset %zu: %s", pos_, what);
    throw ParseError(buf);
  }

  void skip_ws() {
    while (pos_ < t_.size()) {
      char c = t_[pos_];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
      else break;
    }
  }

  bool consume_literal(std::string_view lit) {
    if (t_.substr(pos_, lit.size()) == lit) { pos_ += lit.size(); return true; }
    return false;
  }

  Value parse_value(int depth) {
    if (depth > max_depth_) fail("nesting too deep");
    if (pos_ >= t_.size()) fail("unexpected end of input");
    char c = t_[pos_];
    switch (c) {
      case '{': return parse_object(depth);
      case '[': return parse_array(depth);
      case '"': return Value::string(parse_string());
      case 't': if (consume_literal("true")) return Value::boolean(true); fail("bad literal");
      case 'f': if (consume_literal("false")) return Value::boolean(false); fail("bad literal");
      case 'n': if (consume_literal("null")) return Value::null(); fail("bad literal");
      default:
        if (c == '-' || (c >= '0' && c <= '9')) return Value::number(parse_number());
        fail("unexpected character");
    }
  }

  Value parse_object(int depth) {
    ++pos_;
    std::vector<Value::Member> members;
    skip_ws();
    if (pos_ < t_.size() && t_[pos_] == '}') { ++pos_; return Value::object(std::move(members)); }
    for (;;) {
      skip_ws();
      if (pos_ >= t_.size() || t_[pos_] != '"') fail("expected object key");
      std::string key = parse_string();
      skip_ws();
      if (pos_ >= t_.size() || t_[pos_] != ':') fail("expected ':'");
      ++pos_;
      skip_ws();
      Value v = parse_value(depth + 1);
      members.emplace_back(std::move(key), std::move(v));
      skip_ws();
      if (pos_ >= t_.size()) fail("unterminated object");
      if (t_[pos_] == ',') { ++pos_; continue; }
      if (t_[pos_] == '}') { ++pos_; break; }
      fail("expected ',' or '}'");
    }
    return Value::object(std::move(members));
  }

  Value parse_array(int depth) {
    ++pos_;
    std::vector<Value> items;
    skip_ws();
    if (pos_ < t_.size() && t_[pos_] == ']') { ++pos_; return Value::array(std::move(items)); }
    for (;;) {
      skip_ws();
      items.push_back(parse_value(depth + 1));
      skip_ws();
      if (pos_ >= t_.size()) fail("unterminated array");
      if (t_[pos_] == ',') { ++pos_; continue; }
      if (t_[pos_] == ']') { ++pos_; break; }
      fail("expected ',' or ']'");
    }
    return Value::array(std::move(items));
  }

  static void append_utf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) {
      out.push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
      out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
      out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
      out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
      out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
      out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
      out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
  }

  uint32_t parse_hex4() {
    if (pos_ + 4 > t_.size()) fail("truncated \\u escape");
    uint32_t v = 0;
    for (int i = 0; i < 4; ++i) {
      char c = t_[pos_++];
      v <<= 4;
      if (c >= '0' && c <= '9') v |= static_cast<uint32_t>(c - '0');
      else if (c >= 'a' && c <= 'f') v |= static_cast<uint32_t>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v |= static_cast<uint32_t>(c - 'A' + 10);
      else fail("bad hex digit in \\u escape");
    }
    return v;
  }

  std::string parse_string() {
    ++pos_;
    std::string out;
    for (;;) {
      if (pos_ >= t_.size()) fail("unterminated string");
      char c = t_[pos_++];
      if (c == '"') break;
      if (static_cast<unsigned char>(c) < 0x20) fail("control character in string");
      if (c != '\\') { out.push_back(c); continue; }
      if (pos_ >= t_.size()) fail("unterminated escape");
      char e = t_[pos_++];
      switch (e) {
        case '"': out.push_back('"'); break;
        case '\\': out.push_back('\\'); break;
        case '/': out.push_back('/'); break;
        case 'b': out.push_back('\b'); break;
        case 'f': out.push_back('\f'); break;
        case 'n': out.push_back('\n'); break;
        case 'r': out.push_back('\r'); break;
        case 't': out.push_back('\t'); break;
        case 'u': {
          uint32_t cp = parse_hex4();
          if (cp >= 0xD800 && cp <= 0xDBFF) {
            if (pos_ + 2 > t_.size() || t_[pos_] != '\\' || t_[pos_ + 1] != 'u') fail("lone surrogate");
            pos_ += 2;
            uint32_t lo = parse_hex4();
            if (lo < 0xDC00 || lo > 0xDFFF) fail("bad low surrogate");
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
          } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
            fail("lone low surrogate");
          }
          append_utf8(out, cp);
          break;
        }
        default: fail("unknown escape");
      }
    }
    return out;
  }

  double parse_number() {
    const size_t start = pos_;
    auto digit = [&](size_t p) { return p < t_.size() && t_[p] >= '0' && t_[p] <= '9'; };
    if (t_[pos_] == '-') ++pos_;
    if (!digit(pos_)) fail("expected digit");
    if (t_[pos_] == '0') { ++pos_; }
    else { while (digit(pos_)) ++pos_; }
    if (pos_ < t_.size() && t_[pos_] == '.') {
      ++pos_;
      if (!digit(pos_)) fail("expected digit after '.'");
      while (digit(pos_)) ++pos_;
    }
    if (pos_ < t_.size() && (t_[pos_] == 'e' || t_[pos_] == 'E')) {
      ++pos_;
      if (pos_ < t_.size() && (t_[pos_] == '+' || t_[pos_] == '-')) ++pos_;
      if (!digit(pos_)) fail("expected exponent digits");
      while (digit(pos_)) ++pos_;
    }
    char buf[64];
    const size_t len = pos_ - start;
    if (len >= sizeof buf) fail("number too long");
    std::memcpy(buf, t_.data() + start, len);
    buf[len] = '\0';
    return std::strtod(buf, nullptr);
  }

  std::string_view t_;
  size_t pos_ = 0;
  int max_depth_;
};

}

Value parse(std::string_view text, int max_depth) { return Parser(text, max_depth).parse_document(); }

void Writer::separator() {
  if (after_key_) { after_key_ = false; return; }
  if (!has_items_.empty()) {
    if (has_items_.back()) out_.push_back(',');
    has_items_.back() = true;
  }
}

Writer& Writer::begin_object() { separator(); out_.push_back('{'); has_items_.push_back(false); return *this; }
Writer& Writer::end_object() { out_.push_back('}'); has_items_.pop_back(); return *this; }
Writer& Writer::begin_array() { separator(); out_.push_back('['); has_items_.push_back(false); return *this; }
Writer& Writer::end_array() { out_.push_back(']'); has_items_.pop_back(); return *this; }

Writer& Writer::key(std::string_view k) {
  separator();
  out_.push_back('"');
  escape(k);
  out_.append("\":");
  after_key_ = true;
  return *this;
}

Writer& Writer::number(double v, int precision) {
  separator();
  if (!std::isfinite(v)) { out_.append("null"); return *this; }
  if (std::fabs(v) < 1e15 && v == std::floor(v)) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.0f", v);
    out_.append(buf);
    return *this;
  }
  char buf[48];
  if (std::fabs(v) >= 1e15 || (std::fabs(v) > 0 && std::fabs(v) < 1e-6)) {
    std::snprintf(buf, sizeof buf, "%.*g", precision + 2, v);
    out_.append(buf);
    return *this;
  }
  int n = std::snprintf(buf, sizeof buf, "%.*f", precision, v);
  while (n > 1 && buf[n - 1] == '0') --n;
  if (n > 1 && buf[n - 1] == '.') --n;
  if (n == 2 && buf[0] == '-' && buf[1] == '0') { out_.push_back('0'); return *this; }
  out_.append(buf, static_cast<size_t>(n));
  return *this;
}

Writer& Writer::integer(int64_t v) {
  separator();
  char buf[32];
  std::snprintf(buf, sizeof buf, "%lld", static_cast<long long>(v));
  out_.append(buf);
  return *this;
}

Writer& Writer::boolean(bool v) { separator(); out_.append(v ? "true" : "false"); return *this; }
Writer& Writer::null() { separator(); out_.append("null"); return *this; }

Writer& Writer::string(std::string_view v) {
  separator();
  out_.push_back('"');
  escape(v);
  out_.push_back('"');
  return *this;
}

Writer& Writer::raw(std::string_view json) { separator(); out_.append(json); return *this; }

void Writer::escape(std::string_view s) {
  static const char* hex = "0123456789abcdef";
  for (char c : s) {
    switch (c) {
      case '"': out_.append("\\\""); break;
      case '\\': out_.append("\\\\"); break;
      case '\n': out_.append("\\n"); break;
      case '\r': out_.append("\\r"); break;
      case '\t': out_.append("\\t"); break;
      case '\b': out_.append("\\b"); break;
      case '\f': out_.append("\\f"); break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          out_.append("\\u00");
          out_.push_back(hex[(c >> 4) & 0xF]);
          out_.push_back(hex[c & 0xF]);
        } else {
          out_.push_back(c);
        }
    }
  }
}

}
