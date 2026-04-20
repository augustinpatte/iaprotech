import React from "react"
import ReactDOM from "react-dom/client"
import { BrowserRouter } from "react-router-dom"
import i18n from "i18next"
import LanguageDetector from "i18next-browser-languagedetector"
import { initReactI18next } from "react-i18next"
import App from "./App.jsx"
import "./index.css"
import fr from "./locales/fr.json"
import en from "./locales/en.json"
import es from "./locales/es.json"

const LANGUAGE_STORAGE_KEY = "language"
const SUPPORTED_LANGUAGES = ["fr", "en", "es"]

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
  resources: {
    fr: { translation: fr },
    en: { translation: en },
    es: { translation: es },
  },
  fallbackLng: "fr",
  supportedLngs: SUPPORTED_LANGUAGES,
  detection: {
    order: ["localStorage", "navigator"],
    lookupLocalStorage: LANGUAGE_STORAGE_KEY,
    caches: ["localStorage"],
  },
  interpolation: { escapeValue: false },
})

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
)
