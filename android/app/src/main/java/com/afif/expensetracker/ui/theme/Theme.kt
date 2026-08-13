package com.afif.expensetracker.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.shape.RoundedCornerShape

/** Core surfaces and semantic colors shared by the ledger screens. */
val Ink = Color(0xFF08110F)
val Surface = Color(0xFF101B17)
val Elevated = Color(0xFF182720)
val Mint = Color(0xFF65E6A3)
val Expense = Color(0xFFFF869A)
val Warning = Color(0xFFF6CF69)
val Income = Color(0xFF63D8CE)
val ChartAccent = Color(0xFFB6A4FF)

private val LedgerColors = darkColorScheme(
    primary = Mint,
    onPrimary = Color(0xFF062117),
    primaryContainer = Color(0xFF1D5740),
    onPrimaryContainer = Color(0xFFB9F5D2),
    inversePrimary = Color(0xFF226B4B),
    secondary = Color(0xFFA7CDBB),
    onSecondary = Color(0xFF10271D),
    secondaryContainer = Color(0xFF29483A),
    onSecondaryContainer = Color(0xFFC5EBD7),
    tertiary = ChartAccent,
    onTertiary = Color(0xFF21164D),
    tertiaryContainer = Color(0xFF42376D),
    onTertiaryContainer = Color(0xFFE9DEFF),
    background = Ink,
    onBackground = Color(0xFFE9F4EE),
    surface = Surface,
    onSurface = Color(0xFFE9F4EE),
    surfaceVariant = Color(0xFF24332C),
    onSurfaceVariant = Color(0xFFB5C5BB),
    surfaceTint = Mint,
    inverseSurface = Color(0xFFE9F4EE),
    inverseOnSurface = Color(0xFF1A211D),
    error = Expense,
    onError = Color(0xFF3B0716),
    errorContainer = Color(0xFF6B2033),
    onErrorContainer = Color(0xFFFFD9DE),
    outline = Color(0xFF7F9186),
    outlineVariant = Color(0xFF3B4B43),
    scrim = Color.Black,
    surfaceDim = Color(0xFF0D1713),
    surfaceBright = Color(0xFF25352D),
    surfaceContainerLowest = Color(0xFF050B09),
    surfaceContainerLow = Color(0xFF121F1A),
    surfaceContainer = Elevated,
    surfaceContainerHigh = Color(0xFF1E2D25),
    surfaceContainerHighest = Color(0xFF293930),
)

/** Typography keeps amounts aligned and easy to scan with tabular numerals. */
val LedgerTypography = Typography(
    displayLarge = TextStyle(
        fontSize = 36.sp,
        lineHeight = 44.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = (-0.5).sp,
        fontFeatureSettings = "tnum",
    ),
    displayMedium = TextStyle(
        fontSize = 30.sp,
        lineHeight = 38.sp,
        fontWeight = FontWeight.Bold,
        fontFeatureSettings = "tnum",
    ),
    headlineLarge = TextStyle(fontSize = 28.sp, lineHeight = 36.sp, fontWeight = FontWeight.Bold),
    headlineMedium = TextStyle(fontSize = 24.sp, lineHeight = 32.sp, fontWeight = FontWeight.SemiBold),
    titleLarge = TextStyle(fontSize = 22.sp, lineHeight = 28.sp, fontWeight = FontWeight.SemiBold),
    titleMedium = TextStyle(
        fontSize = 16.sp,
        lineHeight = 24.sp,
        fontWeight = FontWeight.SemiBold,
        fontFeatureSettings = "tnum",
    ),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 24.sp, fontFeatureSettings = "tnum"),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontFeatureSettings = "tnum"),
    bodySmall = TextStyle(fontSize = 12.sp, lineHeight = 16.sp),
    labelLarge = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold),
    labelMedium = TextStyle(fontSize = 12.sp, lineHeight = 16.sp, fontWeight = FontWeight.Medium),
    labelSmall = TextStyle(fontSize = 11.sp, lineHeight = 16.sp, fontWeight = FontWeight.Medium),
)

val LedgerShapes = Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(10.dp),
    medium = RoundedCornerShape(16.dp),
    large = RoundedCornerShape(24.dp),
    extraLarge = RoundedCornerShape(28.dp),
)

@Composable
fun LedgerTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = LedgerColors,
        typography = LedgerTypography,
        shapes = LedgerShapes,
        content = content,
    )
}
