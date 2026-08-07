import eslint from "@eslint/js";
import parser from "@typescript-eslint/parser";

export default [
  { ignores: ["packages/**/dist/**"] },
  eslint.configs.recommended,
  {
    files: ["packages/**/*.ts"],
    languageOptions: { parser },
    rules: { "no-undef": "off", "no-unused-vars": "off" },
  },
];
