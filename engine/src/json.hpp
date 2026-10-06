#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace kessler::json {

struct ParseError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

class Value {
 public:
  enum class Type : uint8_t { Null, Bool, Number, String, Array, Object };
  using Member = std::pair<std::string, Value>;

  Value() = default;
  static Value null() { return Value(); }
  static Value boolean(bool b) { Value v; v.type_ = Type::Bool; v.b_ = b; return v; }
  static Value number(double n) { Value v; v.type_ = Type::Number; v.n_ = n; return v; }
  static Value string(std::string s) { Value v; v.type_ = Type::String; v.s_ = std::move(s); return v; }
  static Value array(std::vector<Value> a) { Value v; v.type_ = Type::Array; v.a_ = std::move(a); return v; }
  static Value object(std::vector<Member> o) { Value v; v.type_ = Type::Object; v.o_ = std::move(o); return v; }

  Type type() const { return type_; }
  bool is_null() const { return type_ == Type::Null; }
  bool is_bool() const { return type_ == Type::Bool; }
  bool is_number() const { return type_ == Type::Number; }
  bool is_string() const { return type_ == Type::String; }
  bool is_array() const { return type_ == Type::Array; }
  bool is_object() const { return type_ == Type::Object; }

  bool as_bool() const { return b_; }
  double as_number() const { return n_; }
  const std::string& as_string() const { return s_; }
  const std::vector<Value>& items() const { return a_; }
  const std::vector<Member>& members() const { return o_; }

  const Value* find(std::string_view key) const;

 private:
  Type type_ = Type::Null;
  bool b_ = false;
  double n_ = 0.0;
  std::string s_;
  std::vector<Value> a_;
  std::vector<Member> o_;
};

Value parse(std::string_view text, int max_depth = 64);

const char* type_name(Value::Type t);

class Writer {
 public:
  Writer() { out_.reserve(4096); }

  Writer& begin_object();
  Writer& end_object();
  Writer& begin_array();
  Writer& end_array();
  Writer& key(std::string_view k);
  Writer& number(double v, int precision = 4);
  Writer& integer(int64_t v);
  Writer& boolean(bool v);
  Writer& string(std::string_view v);
  Writer& null();
  Writer& raw(std::string_view json);

  const std::string& str() const { return out_; }
  std::string take() { return std::move(out_); }

 private:
  void separator();
  void escape(std::string_view s);

  std::string out_;
  std::vector<bool> has_items_;
  bool after_key_ = false;
};

}
