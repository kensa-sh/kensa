import eslint from "@eslint/js";
import tseslint from "@typescript-eslint/eslint-plugin";

export default [
  { ignores: ["packages/**/coverage/**", "packages/**/dist/**"] },
  eslint.configs.recommended,
  ...tseslint.configs["flat/recommended-type-checked"],
  {
    files: ["packages/**/*.ts"],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
];
