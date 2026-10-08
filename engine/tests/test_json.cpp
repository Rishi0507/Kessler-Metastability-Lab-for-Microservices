#include "../src/json.hpp"
#include "harness.hpp"

using namespace kessler;

TEST_CASE(json_parses_nested_documents) {
  const json::Value v = json::parse(R"({"a": [1, 2.5, -3e2], "b": {"c": true, "d": null}, "s": "x\"y\u00e9"})");
  REQUIRE(v.is_object());
  const json::Value* a = v.find("a");
  REQUIRE(a && a->is_array() && a->items().size() == 3);
  CHECK_NEAR(a->items()[1].as_number(), 2.5, 1e-12);
  CHECK_NEAR(a->items()[2].as_number(), -300.0, 1e-12);
  CHECK(v.find("b")->find("c")->as_bool());
  CHECK(v.find("b")->find("d")->is_null());
  CHECK(v.find("s")->as_string() == "x\"y\xC3\xA9");
  CHECK(v.find("missing") == nullptr);
}

TEST_CASE(json_handles_surrogate_pairs) {
  const json::Value v = json::parse(R"("\ud83d\ude80")");
  CHECK(v.as_string() == "\xF0\x9F\x9A\x80");
}

TEST_CASE(json_rejects_malformed_input) {
  CHECK_THROWS(json::parse("{"));
  CHECK_THROWS(json::parse("[1,]"));
  CHECK_THROWS(json::parse("{\"a\" 1}"));
  CHECK_THROWS(json::parse("01"));
  CHECK_THROWS(json::parse("\"unterminated"));
  CHECK_THROWS(json::parse("[1] trailing"));
  CHECK_THROWS(json::parse("\"\\ud800\""));
  CHECK_THROWS(json::parse(std::string(200, '[') + std::string(200, ']')));
}

TEST_CASE(json_writer_formats_compactly) {
  json::Writer w;
  w.begin_object();
  w.key("i").number(42.0);
  w.key("f").number(3.14159, 2);
  w.key("trim").number(2.50, 4);
  w.key("neg0").number(-0.00001, 2);
  w.key("nan").number(std::nan(""));
  w.key("s").string("a\"b\n");
  w.key("arr").begin_array().integer(1).boolean(false).null().end_array();
  w.end_object();
  CHECK(w.str() == R"({"i":42,"f":3.14,"trim":2.5,"neg0":0,"nan":null,"s":"a\"b\n","arr":[1,false,null]})");
}

TEST_CASE(json_writer_roundtrips_through_parser) {
  json::Writer w;
  w.begin_array().string("\x01\t").number(1e-9, 3).number(12345678.9, 1).end_array();
  const json::Value v = json::parse(w.str());
  REQUIRE(v.is_array());
  CHECK(v.items()[0].as_string() == "\x01\t");
  CHECK_NEAR(v.items()[1].as_number(), 1e-9, 1e-12);
  CHECK_NEAR(v.items()[2].as_number(), 12345678.9, 1e-6);
}
