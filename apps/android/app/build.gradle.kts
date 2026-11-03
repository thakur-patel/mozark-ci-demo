plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Version comes from the git tag in CI (v0.1.0 -> 0.1.0).
val appVersion = (System.getenv("APP_VERSION") ?: "0.0.0-dev").removePrefix("v")
val appVersionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

android {
    namespace = "com.mozark.cidemo"
    compileSdk = 34
    defaultConfig {
        applicationId = "com.mozark.cidemo"
        minSdk = 24
        targetSdk = 34
        versionCode = appVersionCode
        versionName = appVersion
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
